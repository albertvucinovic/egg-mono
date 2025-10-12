#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.live import Live

# Prompt session for robust input while we use Rich for output/streaming
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eggthreads'))
from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    create_root_thread,
    create_child_thread,
    append_message,
    delete_thread,
    is_thread_runnable,
    list_threads,
    list_root_threads,
    get_parent,
    list_children_with_meta,
    list_children_ids,
    current_open_invoke,
    create_snapshot,
    interrupt_thread,
    pause_thread,
    resume_thread,
)

# Import completer after eggthreads path is on sys.path so completion can import eggthreads APIs
from completion import EggCompleter
from eggthreads.event_watcher import EventWatcher


MODELS_PATH = Path(__file__).resolve().parent / 'models.json'
ALL_MODELS_PATH = Path(__file__).resolve().parent / 'all-models.json'
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / 'systemPrompt'

# For model listing/completion via eggllm (no network here)
import sys as _sys2
# Ensure local sibling libraries are importable
_sys2.path.insert(0, str(Path(__file__).resolve().parent.parent / 'eggllm'))
try:
    from eggllm import LLMClient  # type: ignore
except Exception:
    LLMClient = None  # type: ignore



def _render_message_panel(m: Dict[str, Any]) -> Optional[Panel]:
    role = m.get('role')
    content = (m.get('content') or '').strip()
    model_key = m.get('model_key') or ''

    # Promote error-looking messages for visibility
    if role == 'system' and isinstance(content, str) and content.lower().startswith('llm error:'):
        title = '[bold red]Error[/bold red]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        return Panel(Text(content, no_wrap=False, overflow='fold', style='red'), title=title, border_style='red')

    if role == 'user':
        title = "[bold green]User[/bold green]"
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        body = content if content else ''
        return Panel(Text(body, no_wrap=False, overflow='fold', style='green'), title=title, border_style='green')

    elif role == 'assistant':
        title = '[bold cyan]Assistant[/bold cyan]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        border = 'cyan'
        # If no visible content but tool_calls/reasoning exist, render a helpful summary
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
                return None
            content = "\n\n".join(pieces)
        # Markdown for assistant if it looks like markdown
        if _looks_markdown(content):
            renderable = Markdown(content)
        else:
            renderable = Text(content, no_wrap=False, overflow='fold', style='cyan')

    elif role == 'tool':
        name = m.get('name') or 'Tool'
        title = f'[bold yellow]{name}[/bold yellow]'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        border = 'yellow'
        renderable = Text(content, no_wrap=False, overflow='fold', style='yellow')

    else:
        title = role or 'Message'
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        border = 'blue'
        renderable = Text(content, no_wrap=False, overflow='fold', style='blue')

    return Panel(renderable, title=title, border_style=border)


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
    # BFS to get subtree threads
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
    return out[1:]  # exclude root


def _render_static_view(db: ThreadsDB, thread_id: str) -> None:
    console = Console(force_terminal=True, color_system='auto', no_color=False)
    msgs = _snapshot_messages(db, thread_id)
    if not msgs:
        console.print(Panel('[dim]No messages yet[/dim]', border_style='blue'))
        return
    # Show a generous slice of recent history so switching threads gives context
    for m in msgs[-50:]:
        # If assistant has stream_sequence, honor streaming order when rendering
        if isinstance(m, dict) and m.get('role') == 'assistant' and isinstance(m.get('stream_sequence'), list) and m.get('stream_sequence'):
            seq = m.get('stream_sequence') or []
            # Group adjacent same-type (and same-name for tool entries)
            grouped = []
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
            # Render in grouped streaming order
            for g in grouped:
                gtype = g.get('type')
                if gtype == 'reason':
                    console.print(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title='Reasoning', border_style='magenta'))
                elif gtype == 'tool_call_args':
                    nm = g.get('name') or 'tool'
                    console.print(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title=f'Tool Call Args: {nm}', border_style='yellow'))
                elif gtype == 'tool_output':
                    nm = g.get('name') or 'tool'
                    console.print(Panel(Text(g.get('text',''), no_wrap=False, overflow='fold'), title=f'Tool: {nm}', border_style='yellow'))
                elif gtype == 'content':
                    m2 = dict(m)
                    m2['content'] = g.get('text','')
                    panel = _render_message_panel(m2)
                    if panel:
                        console.print(panel)
            # After rendering by sequence, still show final tool_calls summary if present
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
                if out_lines:
                    console.print(Panel(Text("\n".join(out_lines), no_wrap=False, overflow='fold'), title='Tool Calls', border_style='yellow'))
            continue

        # For assistant messages without explicit stream_sequence, prefer rendering Reasoning before content
        if isinstance(m, dict) and m.get('role') == 'assistant':
            reas = m.get('reasoning') or m.get('reasoning_content')
            has_content = bool((m.get('content') or '').strip())
            if has_content and isinstance(reas, str) and reas.strip():
                console.print(Panel(Text(reas, no_wrap=False, overflow='fold'), title='Reasoning', border_style='magenta'))
            panel = _render_message_panel(m)
            if panel:
                console.print(panel)
            # If the assistant message has streamed-only metadata, show those too
            # Show tool_calls if present
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
                if out_lines:
                    console.print(Panel(Text("\n".join(out_lines), no_wrap=False, overflow='fold'), title='Tool Calls', border_style='yellow'))
            # Show tool outputs if we captured their stream in snapshot metadata
            tstream = m.get('tool_stream') or {}
            if isinstance(tstream, dict) and tstream:
                for nm, txt in tstream.items():
                    if txt:
                        console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Output: {nm}', border_style='yellow'))
            # Show streamed tool-call arguments if captured
            tc_stream = m.get('tool_calls_stream') or {}
            if isinstance(tc_stream, dict) and tc_stream:
                for nm, txt in tc_stream.items():
                    if txt:
                        console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Call Args (streamed): {nm}', border_style='yellow'))
        else:
            panel = _render_message_panel(m)
            if panel:
                console.print(panel)




async def _show_thread_ui(db: ThreadsDB, thread_id: str) -> None:
    """Render the selected thread and, if a live stream is active, attach to it.

    - Prints a static view of the latest messages for quick context
    - If open_streams has an active invoke for this thread, attaches to
      the stream to show live deltas until it closes.
    """
    console = Console(force_terminal=True, color_system='auto')
    console.clear()

    # Check if a live stream is already open BEFORE snapshotting.
    # If a stream is active, do not refresh the snapshot so that all
    # post-snapshot events are processed as part of the streaming view.
    row_open = None
    try:
        row_open = db.current_open(thread_id)
    except Exception:
        row_open = None

    if row_open is None:
        # Freshen snapshot only when not streaming
        try:
            create_snapshot(db, thread_id)
        except Exception:
            pass

    console.print(Panel(f'Switched to thread: {thread_id}', border_style='blue'))
    _render_static_view(db, thread_id)

    # Attach to ongoing stream if present, or briefly wait for one to start
    row = row_open
    if row is None:
        # Poll briefly in case a scheduler is about to start streaming
        import asyncio as _asyncio
        for _ in range(20):  # ~1s @ 50ms
            try:
                row = db.current_open(thread_id)
            except Exception:
                row = None
            if row is not None:
                break
            await _asyncio.sleep(0.05)
    # Only attach if the thread is runnable or is already streaming
    if row is not None:
        console.print(Panel('[dim]Attaching to live stream...[/dim]', border_style='cyan'))
        await _stream_thread(db, thread_id)

async def _stream_thread(db: ThreadsDB, thread_id: str) -> None:
    console = Console(force_terminal=True, color_system='auto')

    # Start watching from last persisted snapshot event to capture the new turn's stream.open and deltas
    try:
        th = db.get_thread(thread_id)
        start_after = int(th.snapshot_last_event_seq) if th and isinstance(th.snapshot_last_event_seq, int) else -1
    except Exception:
        start_after = -1

    # Determine current open invoke, if any
    active_target: Optional[str] = None
    try:
        row_open = db.current_open(thread_id)
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

    # If a stream is open, and the stream.open happened before the snapshot,
    # attach from the stream.open (so we don't miss the beginning of the turn).
    attach_after_seq = start_after
    if active_target:
        try:
            row_open_seq = db.conn.execute(
                "SELECT MIN(event_seq) FROM events WHERE invoke_id=? AND type='stream.open'",
                (active_target,)
            ).fetchone()
            open_seq = int(row_open_seq[0]) if row_open_seq and row_open_seq[0] is not None else None
            if open_seq is not None:
                # Watch from just before stream.open to include it in the stream
                attach_after_seq = min(start_after, open_seq - 1) if start_after >= 0 else (open_seq - 1)
        except Exception:
            pass

    # If there are any events after the chosen boundary for the active invoke,
    # pre-load them into the live buffers so that attaching mid-stream shows the
    # full live message (reasoning, tool args/output, content) seamlessly.
    def _preload_existing(after_seq: int, target_invoke: Optional[str]):
        last_seq = after_seq
        live_content_local = ''
        live_reason_local = ''
        tool_stream_local: Dict[str, str] = {}
        tc_panels_order_local: List[str] = []
        tc_text_local: Dict[str, str] = {}
        tc_map_local: Dict[str, List[str]] = {}
        seen_open = False
        try:
            cur = db.conn.execute(
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
            # If we know the active target, only preload for that invoke
            if target_invoke is not None and inv != target_invoke:
                continue
            if t == 'stream.open':
                # Start fresh for the target invoke
                seen_open = True
                live_content_local = ''
                live_reason_local = ''
                tool_stream_local.clear()
                tc_panels_order_local.clear()
                tc_text_local.clear()
                tc_map_local.clear()
            elif t == 'stream.delta' and (seen_open or target_invoke):
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                txt = payload.get('text') or payload.get('delta') or payload.get('content')
                if isinstance(txt, str) and txt:
                    live_content_local += txt
                rs = payload.get('reason')
                if isinstance(rs, str) and rs:
                    live_reason_local += rs
                tl = payload.get('tool')
                if isinstance(tl, dict):
                    nm = tl.get('name') or 'tool'
                    tool_stream_local[nm] = tool_stream_local.get(nm, '') + (tl.get('text') or '')
                tcd = payload.get('tool_call')
                if isinstance(tcd, dict):
                    raw_key = str(tcd.get('id') or tcd.get('name') or 'tool')
                    frag = tcd.get('text') or tcd.get('arguments_delta') or ''
                    if frag is None:
                        frag = ''
                    if isinstance(frag, str) and frag:
                        existing_keys = tc_map_local.get(raw_key, [])
                        current_pkey = existing_keys[-1] if existing_keys else None
                        if not current_pkey:
                            new_pkey = f"{raw_key}-{len(tc_panels_order_local)}"
                            tc_panels_order_local.append(new_pkey)
                            tc_text_local[new_pkey] = ''
                            tc_map_local.setdefault(raw_key, []).append(new_pkey)
                            current_pkey = new_pkey
                        tc_text_local[current_pkey] += frag
            elif t == 'stream.close':
                # If the active invoke closed, stop preloading here
                if (target_invoke is None) or (inv == target_invoke):
                    return {
                        'last_seq': last_seq,
                        'content': live_content_local,
                        'reason': live_reason_local,
                        'tool_stream': tool_stream_local,
                        'tc_panels_order': tc_panels_order_local,
                        'tc_text': tc_text_local,
                        'tc_map': tc_map_local,
                        'active_invoke': None,
                    }
        return {
            'last_seq': last_seq,
            'content': live_content_local,
            'reason': live_reason_local,
            'tool_stream': tool_stream_local,
            'tc_panels_order': tc_panels_order_local,
            'tc_text': tc_text_local,
            'tc_map': tc_map_local,
            'active_invoke': target_invoke,
        }

    preload = _preload_existing(attach_after_seq, active_target)

    # Use the last preloaded event as the starting point for live watching
    after_for_watch = preload.get('last_seq', attach_after_seq) if isinstance(preload, dict) else attach_after_seq
    ew = EventWatcher(db, thread_id, after_seq=after_for_watch, poll_sec=0.05)

    # Track the currently active invoke, if any, so we can attach mid-stream
    active_invoke: Optional[str] = preload.get('active_invoke') if isinstance(preload, dict) else None

    live_content = preload.get('content', '') if isinstance(preload, dict) else ''
    live_reason = preload.get('reason', '') if isinstance(preload, dict) else ''
    # tool streaming buffers: name -> text
    tool_stream: Dict[str, str] = preload.get('tool_stream', {}) if isinstance(preload, dict) else {}
    # tool-call args streaming panels: maintain ordered panels to avoid reusing old panel
    tc_panels_order: List[str] = preload.get('tc_panels_order', []) if isinstance(preload, dict) else []
    tc_text: Dict[str, str] = preload.get('tc_text', {}) if isinstance(preload, dict) else {}
    tc_map: Dict[str, List[str]] = preload.get('tc_map', {}) if isinstance(preload, dict) else {}

    # Keep a reference to the latest polled batch for model label detection
    batch: List[Dict[str, Any]] = []

    def _live_group() -> Group:
        panels = []
        # Only show streaming panels here; static panels are printed before entering Live
        # Determine model from the latest stream events (scan a few recent events)
        model_label = ''
        try:
            # find the most recent model_key from stream events for this invoke
            for e in reversed(batch[-10:] if batch else []):
                try:
                    pj = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                except Exception:
                    pj = {}
                mk = pj.get('model_key')
                if isinstance(mk, str) and mk.strip():
                    model_label = mk.strip()
                    break
        except Exception:
            model_label = ''

        if live_reason.strip():
            title = 'Reasoning (streaming)'
            if model_label:
                title += f" [dim](model: {model_label})[/dim]"
            panels.append(Panel(Text(live_reason, no_wrap=False, overflow='fold'), title=title, border_style='magenta'))
        # show tool-call arguments panels in creation order
        for pkey in tc_panels_order:
            delta = tc_text.get(pkey, '').strip()
            if delta:
                title = f'Tool Call Args: {pkey}'
                if model_label:
                    title += f" [dim](model: {model_label})[/dim]"
                panels.append(Panel(Text(delta, no_wrap=False, overflow='fold'), title=title, border_style='yellow'))
        for name, txt in tool_stream.items():
            if txt.strip():
                title = f'Tool: {name} (streaming)'
                if model_label:
                    title += f" [dim](model: {model_label})[/dim]"
                panels.append(Panel(Text(txt, no_wrap=False, overflow='fold'), title=title, border_style='yellow'))
        if live_content.strip():
            title = 'Assistant (streaming)'
            if model_label:
                title += f" [dim](model: {model_label})[/dim]"
            panels.append(Panel(Text(live_content, no_wrap=False, overflow='fold'), title=title, border_style='cyan'))
        return Group(*panels) if panels else Group(Panel('[dim]No messages yet[/dim]', border_style='blue'))

    # In-place streaming
    with Live(console=console, auto_refresh=False, vertical_overflow='visible') as live:
        # Initial paint reflects any preloaded deltas
        live.update(_live_group(), refresh=True)
        while True:
            updated = False
            batch = []
            async for b in ew.aiter():
                batch = b
                break  # take one batch per loop
            for e in batch:
                t = e['type']
                if t == 'stream.open':
                    active_invoke = e['invoke_id']
                    live_content = ''
                    live_reason = ''
                    tool_stream.clear()
                    tc_panels_order.clear()
                    tc_text.clear()
                    tc_map.clear()
                    # reset any per-invoke trackers if needed
                    updated = True
                elif t == 'stream.delta' and active_invoke and e['invoke_id'] == active_invoke:
                    payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
                    txt = payload.get('text') or payload.get('delta') or payload.get('content')
                    if isinstance(txt, str) and txt:
                        live_content += txt
                        updated = True
                    rs = payload.get('reason')
                    if isinstance(rs, str) and rs:
                        live_reason += rs
                        updated = True
                    tl = payload.get('tool')
                    if isinstance(tl, dict):
                        nm = tl.get('name') or 'tool'
                        tool_stream[nm] = tool_stream.get(nm, '') + (tl.get('text') or '')
                        updated = True
                    tcd = payload.get('tool_call')
                    if isinstance(tcd, dict):
                        raw_key = str(tcd.get('id') or tcd.get('name') or 'tool')
                        frag = tcd.get('text') or tcd.get('arguments_delta') or ''
                        if frag is None:
                            frag = ''
                        if isinstance(frag, str) and frag:
                            # determine current panel for this raw_key
                            existing_keys = tc_map.get(raw_key, [])
                            current_pkey = existing_keys[-1] if existing_keys else None
                            # start a new panel if no current panel
                            if not current_pkey:
                                new_pkey = f"{raw_key}-{len(tc_panels_order)}"
                                tc_panels_order.append(new_pkey)
                                tc_text[new_pkey] = ''
                                tc_map.setdefault(raw_key, []).append(new_pkey)
                                current_pkey = new_pkey
                            # append text (assume incremental deltas)
                            tc_text[current_pkey] += frag
                            updated = True
                elif t == 'stream.close' and active_invoke and e['invoke_id'] == active_invoke:
                    active_invoke = None
                    updated = True
                    # finalize this invoke's live display and return to caller
                    live.update(_live_group(), refresh=True)
                    return
            if updated:
                live.update(_live_group(), refresh=True)
            # If no new events, sleep briefly before next poll
            if not batch:
                await asyncio.sleep(0.05)

async def run_cli():
    console = Console(force_terminal=True, color_system='auto', no_color=False)
    db = ThreadsDB()
    db.init_schema()

    system_content = _get_system_prompt()

    root = create_root_thread(db, name='Root')
    append_message(db, root, 'system', system_content)
    create_snapshot(db, root)
    current_thread = root

    # Track active subtree schedulers (root_thread_id -> {scheduler, task})
    active_schedulers: Dict[str, Dict[str, Any]] = {}
    # Track roots we already prompted user about (to avoid repeated prompts)
    prompted_roots: set[str] = set()
    # Guard to avoid concurrent/re-entrant prompting
    is_prompting_scheduler: bool = False

    # Helper to start a scheduler for a given root thread
    def _start_scheduler(root_tid: str) -> None:
        if root_tid in active_schedulers:
            return
        sched = SubtreeScheduler(db, root_thread_id=root_tid, models_path=str(MODELS_PATH), all_models_path=str(ALL_MODELS_PATH))
        task = asyncio.create_task(sched.run_forever())
        active_schedulers[root_tid] = {"scheduler": sched, "task": task}

    # Start default scheduler on the initial root immediately
    _start_scheduler(root)

    # prompt
    def _current_model_for_thread(tid: str) -> Optional[str]:
        # Scan recent msg.create events for a model_key
        try:
            rows = db.conn.execute(
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
        th = db.get_thread(tid)
        return th.initial_model_key if th else None

    # Helper functions for thread roots, tree rendering, and ensuring schedulers
    def _thread_root_id(tid: str) -> str:
        cur_id = tid
        while True:
            row = db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (cur_id,)).fetchone()
            if not row or not row[0]:
                return cur_id
            cur_id = row[0]

    def _is_streaming(tid: str) -> bool:
        try:
            return db.current_open(tid) is not None
        except Exception:
            return False

    def _is_root_thread(tid: str) -> bool:
        try:
            row = db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (tid,)).fetchone()
            return not (row and row[0])
        except Exception:
            return False

    def _root_has_scheduler(tid: str) -> bool:
        try:
            rid = _thread_root_id(tid)
            return rid in active_schedulers
        except Exception:
            return False

    def _format_thread_line(tid: str) -> str:
        th = db.get_thread(tid)
        status = th.status if th else 'unknown'
        recap = (th.short_recap if th and th.short_recap else 'No recap').strip()
        mk = _current_model_for_thread(tid) or 'default'
        streaming = _is_streaming(tid)
        try:
            subtree_size = len(_get_subtree(db, tid))
        except Exception:
            subtree_size = 0
        label = th.name if th and th.name else ''
        id_short = tid[-8:]
        sflag = '[bold yellow]STREAMING[/bold yellow] ' if streaming else ''
        cur_tag = '[bold cyan][CUR][/bold cyan] ' if tid == current_thread else ''
        sched_tag = '[bold cyan][SCHED][/bold cyan] ' if _is_root_thread(tid) and _root_has_scheduler(tid) else ''
        return f"{cur_tag}{sched_tag}{sflag}[dim]{id_short}[/dim] {status} - {recap} (subtree={subtree_size}) [dim][model: {mk}][/dim]" + (f"  [dim]{label}[/dim]" if label else '')

    def _render_tree(root_tid: str, prefix: str = '', is_last: bool = True) -> None:
        connector = '└─ ' if is_last else '├─ '
        indent_next = '   ' if is_last else '│  '
        console.print(prefix + connector + _format_thread_line(root_tid))
        # List children ordered by created_at for readability
        try:
            kids = [cid for cid, _n, _r, _c in list_children_with_meta(db, root_tid)]
        except Exception:
            kids = []
        for i, cid in enumerate(kids):
            last = (i == len(kids) - 1)
            _render_tree(cid, prefix + indent_next, last)

    async def _ensure_scheduler_for_thread(tid: str) -> None:
        nonlocal active_schedulers, prompted_roots, is_prompting_scheduler
        root_tid = _thread_root_id(tid)
        if root_tid in active_schedulers:
            return
        # If we already prompted for this root during this session, do not prompt again.
        if root_tid in prompted_roots:
            return
        # Avoid re-entrant prompts (e.g., triggered during live streaming side-effects)
        if is_prompting_scheduler:
            return
        is_prompting_scheduler = True
        try:
            console.print(Panel(f"No scheduler is running for root [bold]{root_tid}[/bold]. Proposed subtree:", border_style='blue'))
            _render_tree(root_tid)
            # Ask user for confirmation using a separate, short-lived PromptSession
            try:
                confirm_session = PromptSession(message="Start scheduler for this subtree? [y/N] ")
                ans = (await confirm_session.prompt_async()).strip().lower()
            except Exception:
                ans = 'n'
            if ans in ('y', 'yes'):
                _start_scheduler(root_tid)
                console.print(Panel(f"Started scheduler for root {root_tid}", border_style='green'))
                prompted_roots.add(root_tid)
            else:
                console.print(Panel("Skipped starting scheduler.", border_style='yellow'))
                prompted_roots.add(root_tid)
        finally:
            is_prompting_scheduler = False

    # ---- Common helpers (DRY) ----------------------------------------------
    def _select_threads_by_selector(selector: str) -> List[str]:
        """Return thread IDs matching selector using the same logic as /thread.

        Order: exact id > id endswith suffix > id contains > name contains > recap contains.
        Result preserves DB scan order; caller can re-sort by created_at if needed.
        """
        try:
            rows = list_threads(db)
        except Exception:
            rows = []
        sel_l = (selector or '').lower()
        matches: List[str] = []
        # Priority 1: exact id
        for r in rows:
            if r.thread_id == selector:
                matches = [r.thread_id]
                break
        if not matches and sel_l:
            # Priority 2: endswith id suffix
            suf = [r.thread_id for r in rows if r.thread_id.lower().endswith(sel_l)]
            if suf:
                matches = suf
        if not matches and sel_l:
            # Priority 3: id contains
            cont = [r.thread_id for r in rows if sel_l in r.thread_id.lower()]
            if cont:
                matches = cont
        if not matches and sel_l:
            # Priority 4: name contains
            name_matches = [r.thread_id for r in rows if isinstance(r.name, str) and sel_l in r.name.lower()]
            if name_matches:
                matches = name_matches
        if not matches and sel_l:
            # Priority 5: recap contains
            recap_matches = [r.thread_id for r in rows if isinstance(r.short_recap, str) and sel_l in r.short_recap.lower()]
            if recap_matches:
                matches = recap_matches
        return matches

    def _most_recent_root_thread() -> Optional[str]:
        try:
            row2 = db.conn.execute(
                "SELECT thread_id FROM threads WHERE thread_id NOT IN (SELECT child_id FROM children) ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return row2[0] if row2 else None
        except Exception:
            return None
    def prompt_message():
        mk = _current_model_for_thread(current_thread) or 'default'
        return f"You & {mk}: "

    try:
        llm_for_completion = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH) if LLMClient else None
    except Exception:
        llm_for_completion = None

    # Unified completer: preserve /model behavior and add user text/file completions
    def _get_current_thread():
        return current_thread

    session = PromptSession(
        message=prompt_message,
        auto_suggest=AutoSuggestFromHistory(),
        completer=EggCompleter(db, _get_current_thread, llm_for_completion),
    )
    kb = KeyBindings()

    @kb.add('c-c')
    def _(event):
        try:
            interrupt_thread(db, current_thread)
        except Exception:
            pass
        event.app.invalidate()

    # Multiline input support: Ctrl-J (and Alt-Enter) insert a newline without sending
    @kb.add('c-j')
    def _(event):
        event.current_buffer.insert_text('\n')

    @kb.add('escape', 'enter')
    def _(event):
        event.current_buffer.insert_text('\n')

    session.key_bindings = kb

    console.print(Panel('Started chat. Type /help for commands.', border_style='blue'))

    while True:
        # Schedulers are managed per-root via active_schedulers; initial root already started.
        try:
            # Accept multiline pastes and allow Ctrl-J / Alt-Enter to insert newlines
            user_input = (await session.prompt_async())
        except KeyboardInterrupt:
            break
        except EOFError:
            break

        if not user_input.strip():
            continue

        # Handle $$ command first (hidden from API)
        if user_input.startswith('$$') and len(user_input) > 2:
            bash_command = user_input[2:].strip()
            
            # Execute the bash command
            import subprocess
            try:
                result = subprocess.run(bash_command, shell=True, capture_output=True, text=True, cwd=os.getcwd())
                output = result.stdout
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"
                if result.returncode != 0:
                    output += f"\nReturn code: {result.returncode}"
                
                # Create message content with command and output
                message_content = f"Command: {bash_command}\n\nOutput:\n{output}"
                
                console.print(f"[bold yellow]Executing (hidden from API):[/bold yellow] {bash_command}")
                console.print(f"[bold yellow]Output:[/bold yellow]\n{output}")
                # Save with both flags to prevent sending to API and LLM response
                append_message(db, current_thread, 'user', message_content, extra={'no_api': True, 'keep_user_turn': True})
                create_snapshot(db, current_thread)
                
            except Exception as e:
                error_msg = f"Error executing command: {e}"
                console.print(f"[bold red]{error_msg}[/bold red]")
                append_message(db, current_thread, 'user', f"Command: {bash_command}\n\nError: {error_msg}", extra={'no_api': True, 'keep_user_turn': True})
                create_snapshot(db, current_thread)
            continue

        # Handle $ command (visible to LLM but keeps user turn)
        elif user_input.startswith('$') and len(user_input) > 1:
            bash_command = user_input[1:].strip()
            
            # Execute the bash command
            import subprocess
            try:
                result = subprocess.run(bash_command, shell=True, capture_output=True, text=True, cwd=os.getcwd())
                output = result.stdout
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"
                if result.returncode != 0:
                    output += f"\nReturn code: {result.returncode}"
                
                # Create message content with command and output
                message_content = f"Command: {bash_command}\n\nOutput:\n{output}"
                
                console.print(f"[bold cyan]Executing:[/bold cyan] {bash_command}")
                console.print(f"[bold cyan]Output:[/bold cyan]\n{output}")
                # Save with keep_user_turn flag to prevent LLM response
                append_message(db, current_thread, 'user', message_content, extra={'keep_user_turn': True})
                create_snapshot(db, current_thread)
                
            except Exception as e:
                error_msg = f"Error executing command: {e}"
                console.print(f"[bold red]{error_msg}[/bold red]")
                append_message(db, current_thread, 'user', f"Command: {bash_command}\n\nError: {error_msg}", extra={'keep_user_turn': True})
                create_snapshot(db, current_thread)
            continue

        if user_input.startswith('/'):
            parts = user_input[1:].split(None, 1)
            cmd = parts[0]
            arg = parts[1] if len(parts) > 1 else ''
            if cmd == 'help':
                console.print('/model <key>, /updateAllModels <provider>, /pause, /resume, /spawn <text>, /child <pattern>, /parent, /children, /threads, /thread <selector>, /delete <selector>, /new <name>, /schedulers, /quit')
                console.print('$ <command> - Execute bash command (keeps user turn)')
                console.print('$$ <command> - Execute bash command (hidden from API, keeps user turn)')
            elif cmd == 'model':
                if arg:
                    db.append_event(event_id=os.urandom(10).hex(), thread_id=current_thread, type_='msg.create',
                                    msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{arg}]', 'model_key': arg})
                    create_snapshot(db, current_thread)
                else:
                    # Pretty print available models grouped by provider
                    try:
                        llm = llm_for_completion
                        if not llm:
                            console.print('Models not available (llm client not initialized).')
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
                            lines.append("\nTip: type 'all:' to see full provider catalogs (if downloaded). Use 'all:provider:model'.")
                            console.print("[bold]Available models (by provider):[/bold]\n" + "\n".join(lines))
                    except Exception as e:
                        console.print(f"[red]Error listing models: {e}[/red]")
            elif cmd == 'pause':
                pause_thread(db, current_thread)
            elif cmd == 'resume':
                resume_thread(db, current_thread)
            elif cmd == 'spawn':
                # Determine current model to propagate
                def _latest_model_for_thread(tid: str) -> Optional[str]:
                    try:
                        rows = db.conn.execute(
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
                    th = db.get_thread(tid)
                    return th.initial_model_key if th else None

                cur_model = _latest_model_for_thread(current_thread)
                child = create_child_thread(db, current_thread, name='spawn', initial_model_key=cur_model)
                append_message(db, child, 'system', system_content)
                append_message(db, child, 'user', arg or 'Spawned task')
                if cur_model:
                    # Also record the model selection in the child so tooling picks it up
                    db.append_event(event_id=os.urandom(10).hex(), thread_id=child, type_='msg.create',
                                    msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{cur_model}]', 'model_key': cur_model})
                create_snapshot(db, child)
                console.print(Panel(f"Spawned thread: {child}", border_style='green'))
                # Ensure a scheduler exists for the root of the new child
                await _ensure_scheduler_for_thread(child)
            elif cmd == 'new':
                # Create a brand new root thread and switch to it
                new_name = (arg or '').strip() or 'Root'
                try:
                    cur_model_key = _current_model_for_thread(current_thread) or None
                except Exception:
                    cur_model_key = None
                new_root = create_root_thread(db, name=new_name, initial_model_key=cur_model_key)
                # Seed with system prompt
                append_message(db, new_root, 'system', system_content)
                # Record model selection for tooling if we have one
                if cur_model_key:
                    try:
                        db.append_event(event_id=os.urandom(10).hex(), thread_id=new_root, type_='msg.create',
                                        msg_id=os.urandom(10).hex(), payload={'role': 'system', 'content': f'[model:{cur_model_key}]', 'model_key': cur_model_key})
                    except Exception:
                        pass
                create_snapshot(db, new_root)
                console.print(Panel(f"Created new root thread: {new_root}", border_style='green'))
                # Ask to start scheduler for this subtree
                await _ensure_scheduler_for_thread(new_root)
                current_thread = new_root
                await _show_thread_ui(db, current_thread)
            elif cmd == 'delete':
                # Require selector; do not allow deleting current thread directly
                selector = (arg or '').strip()
                if not selector:
                    console.print(Panel('Usage: /delete <thread-id|suffix|name|recap-fragment>', border_style='yellow'))
                    continue
                # Resolve selector using same logic as /thread
                matches = _select_threads_by_selector(selector)
                # If nothing matched, try parsing first token (in case UI inserted display text)
                if not matches and ' ' in selector:
                    sel_first = selector.split()[0]
                    if sel_first:
                        matches = _select_threads_by_selector(sel_first)
                # If still nothing, try suffix match against all threads as a last resort
                if not matches:
                    try:
                        rows_all = list_threads(db)
                        suf = selector.lower()
                        matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
                    except Exception:
                        matches = []
                # Exclude current thread from deletable candidates
                matches = [m for m in matches if m != current_thread]
                if not matches:
                    console.print(Panel('No deletable thread matches selector.', border_style='yellow'))
                    continue
                # If ambiguous, pick most recent by created_at
                if len(matches) > 1:
                    # Map id->created_at and pick max
                    try:
                        rows = list_threads(db)
                        ca = {r.thread_id: r.created_at for r in rows}
                    except Exception:
                        ca = {}
                    matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
                    console.print(Panel(f"Multiple matches, deleting most recent candidate. Candidates: {', '.join(m[-8:] for m in matches[:5])}{'...' if len(matches)>5 else ''}", border_style='yellow'))
                target_tid = matches[0]
                # Ask for confirmation
                try:
                    confirm_session = PromptSession(message=f"Delete thread {target_tid[-8:]} (y/N)? ")
                    ans = (await confirm_session.prompt_async()).strip().lower()
                except Exception:
                    ans = 'n'
                if ans not in ('y', 'yes'):
                    console.print(Panel('Delete cancelled.', border_style='yellow'))
                    continue
                # Perform deletion
                try:
                    delete_thread(db, target_tid)
                except Exception as e:
                    console.print(Panel(f'Error deleting thread: {e}', border_style='red'))
                    continue
                console.print(Panel(f'Thread {target_tid[-8:]} deleted.', border_style='green'))
            elif cmd == 'child':
                patt = (arg or '').lower()
                rows = list_children_with_meta(db, current_thread)
                candidates: List[str] = []
                for child_id, name, recap, _created in rows:
                    if not patt or patt in (name + ' ' + recap + ' ' + child_id).lower():
                        candidates.append(child_id)
                if candidates:
                    # Ensure scheduler for the child's root before switching
                    await _ensure_scheduler_for_thread(candidates[0])
                    root_new = _thread_root_id(candidates[0])
                    if root_new not in active_schedulers and root_new in prompted_roots:
                        console.print(Panel("Scheduler not started for that subtree; staying on current thread.", border_style='yellow'))
                    else:
                        current_thread = candidates[0]
                        # Show the newly selected child's conversation (and live stream if any)
                        await _show_thread_ui(db, current_thread)
                else:
                    console.print('No matching child.')
            elif cmd == 'parent':
                pid = get_parent(db, current_thread)
                if pid:
                    current_thread = pid
                    # Show the parent's conversation (and live stream if any)
                    await _show_thread_ui(db, current_thread)
                else:
                    console.print('Already at root or no parent found.')
            elif cmd == 'children':
                sub = _get_subtree(db, current_thread)
                if sub:
                    # Render as a tree for each direct child
                    try:
                        direct = [cid for cid, _n, _r, _c in list_children_with_meta(db, current_thread)]
                    except Exception:
                        direct = []
                    if not direct:
                        # fallback to flat
                        for tid in sub:
                            console.print(_format_thread_line(tid))
                    else:
                        for i, cid in enumerate(direct):
                            last = (i == len(direct) - 1)
                            _render_tree(cid, prefix='', is_last=last)
                else:
                    console.print('No subthreads.')
            elif cmd == 'threads':
                try:
                    # Find all roots (threads that are not children)
                    roots = list_root_threads(db)
                    if not roots:
                        console.print('No threads found.')
                    else:
                        console.print('[bold]Threads (by subtree):[/bold]')
                        console.print('[dim]Legend: [CUR]=current thread  [SCHED]=local scheduler running  STREAMING=has open stream[/dim]')
                        for i, rid in enumerate(roots):
                            last = (i == len(roots) - 1)
                            _render_tree(rid, prefix='', is_last=last)
                except Exception as e:
                    console.print(f"Error listing threads: {e}")
            elif cmd == 'thread':
                sel = (arg or '').strip()
                if not sel:
                    th = db.get_thread(current_thread)
                    console.print(f"Current thread: {current_thread} name='{(th.name if th else '') or ''}'")
                else:
                    try:
                        matches = _select_threads_by_selector(sel)
                        if not matches and ' ' in sel:
                            sel_first = sel.split()[0]
                            if sel_first:
                                matches = _select_threads_by_selector(sel_first)
                        if not matches:
                            try:
                                rows_all = list_threads(db)
                                suf = sel.lower()
                                matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
                            except Exception:
                                matches = []
                        if not matches:
                            console.print(f"No thread matches selector: {sel}")
                        else:
                            # If ambiguous, choose most recent by created_at
                            if len(matches) > 1:
                                # Map id->created_at and pick max
                                try:
                                    rows = list_threads(db)
                                    ca = {r.thread_id: r.created_at for r in rows}
                                except Exception:
                                    ca = {}
                                matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
                                console.print(f"Multiple matches, switching to most recent. Candidates: {', '.join(m[-8:] for m in matches[:5])}{'...' if len(matches)>5 else ''}")
                            new_tid = matches[0]
                            # Ensure a scheduler exists for the root of the new thread if it's outside current schedulers
                            await _ensure_scheduler_for_thread(new_tid)
                            #Even if no scheduler, we can see thread content (it will not run though)
                            current_thread = new_tid
                            await _show_thread_ui(db, current_thread)
                    except Exception as e:
                        console.print(f"Error switching thread: {e}")
            elif cmd == 'updateAllModels':
                provider = (arg or '').strip()
                if not provider:
                    console.print('Usage: /updateAllModels <provider>')
                else:
                    # Defer to eggllm catalog updater via a lightweight LLMClient
                    try:
                        if not LLMClient:
                            raise RuntimeError('eggllm not available')
                        llm_tmp = LLMClient(models_path=MODELS_PATH, all_models_path=ALL_MODELS_PATH)
                        res = llm_tmp.update_all_models(provider)
                        console.print(Panel(res, border_style='cyan', title='Update All Models'))
                    except Exception as e:
                        console.print(Panel(f'Error: {e}', border_style='red', title='Update All Models'))
            elif cmd == 'schedulers':
                if not active_schedulers:
                    console.print('No active schedulers in this session.')
                else:
                    console.print('[bold]Active SubtreeSchedulers:[/bold]')
                    for rid, ent in active_schedulers.items():
                        console.print(f"- root {rid}:")
                        _render_tree(rid, prefix='   ', is_last=True)
            elif cmd == 'quit':
                break
            else:
                console.print('Unknown command')
            continue

        # Normal user message
        append_message(db, current_thread, 'user', user_input)
        create_snapshot(db, current_thread)
        # Stream response synchronously only if last trigger is runnable
        while True:
            # Check runnable before streaming (avoid running on assistant-only tail)
            try:
                runnable = is_thread_runnable(db, current_thread)
            except Exception:
                runnable = True
            if not runnable:
                break
            try:
                await _stream_thread(db, current_thread)
            except KeyboardInterrupt:
                try:
                    interrupt_thread(db, current_thread)
                except Exception:
                    pass
                break  # Break out of while on interrupt
            # Check if we need to continue
            msgs = _snapshot_messages(db, current_thread)
            if not msgs:
                break
            last = msgs[-1]
            role = last.get('role')
            # Continue automatically if tool execution just happened or assistant had tool_calls
            if role == 'tool' or (role == 'assistant' and last.get('tool_calls')):
                continue
            break

if __name__ == '__main__':
    asyncio.run(run_cli())
