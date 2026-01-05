"""Panel management mixin for the egg application."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich import box as rich_box

from eggthreads import create_snapshot

from utils import snapshot_messages, looks_markdown


class PanelsMixin:
    """Mixin providing panel management methods for EggDisplayApp."""

    def _get_static_box(self) -> Any:
        """Get the box style to use for static console panels.

        Returns MINIMAL when borders are hidden, SQUARE otherwise.
        """
        if getattr(self, '_borders_visible', True):
            return rich_box.SQUARE
        return rich_box.MINIMAL

    def update_panels(self) -> None:
        """Update all UI panels with current state."""
        # Update Chat Messages panel content and title with aggregate
        # token statistics derived from the current snapshot.
        self.chat_output.set_content(self.compose_chat_panel_text())

        try:
            ctx_tokens, api_usage = self.current_token_stats()

            def fmt_tok(v: int) -> str:
                if v < 1000:
                    return str(v)
                return f"{v/1000:.2f}k"

            # If we have no token stats yet for this thread, keep the
            # existing title so that we do not clear previously
            # computed information while a new turn is streaming.
            have_ctx = isinstance(ctx_tokens, int)
            have_api = isinstance(api_usage, dict) and bool(api_usage)
            if not (have_ctx or have_api):
                pass  # leave title unchanged
            else:
                title_parts: List[str] = ["Chat Messages"]
                if have_ctx:
                    title_parts.append(f"ctx≈{fmt_tok(int(ctx_tokens))}")
                cost_str = ""
                if have_api:
                    ti = api_usage.get("total_input_tokens")
                    to = api_usage.get("total_output_tokens")
                    cc = api_usage.get("approx_call_count")
                    cached_in = api_usage.get("cached_input_tokens")
                    segs: List[str] = []
                    if isinstance(ti, int):
                        segs.append(f"in≈{fmt_tok(ti)}")
                    if isinstance(to, int):
                        segs.append(f"out≈{fmt_tok(to)}")
                    if isinstance(cached_in, int) and cached_in > 0:
                        segs.append(f"cached≈{fmt_tok(cached_in)}")
                    if isinstance(cc, int):
                        segs.append(f"calls={cc}")
                    if segs:
                        title_parts.append(" ".join(segs))

                    # Approximate cost (computed by eggthreads.total_token_stats).
                    try:
                        cu = api_usage.get('cost_usd') if isinstance(api_usage.get('cost_usd'), dict) else {}
                        total_cost = float(cu.get('total') or 0.0)
                        if total_cost > 0:
                            cost_str = f"${total_cost:.4f}"
                    except Exception:
                        cost_str = ""

                if cost_str:
                    title_parts.append(f"cost≈{cost_str}")
                self.chat_output.title = "  |  ".join(title_parts)
        except Exception:
            # Leave existing title unchanged on any error.
            pass

        # Update System panel title to reflect sandbox status so the
        # user always has a prominent, persistent indicator.
        try:
            from eggthreads import get_thread_sandbox_status

            sb = get_thread_sandbox_status(self.db, self.current_thread)
        except Exception:
            sb = {}
        try:
            effective = bool(sb.get('effective'))
        except Exception:
            effective = False
        if effective:
            provider = sb.get('provider', 'docker')
            # Map provider to display name
            if provider == 'docker':
                display = 'Docker'
            elif provider == 'srt':
                display = 'srt'
            elif provider == 'bwrap':
                display = 'Bwrap'
            else:
                display = provider
            self.system_output.title = f"System  [green]Sandboxing[{display}][/green]"
        else:
            self.system_output.title = "System  [red]Sandboxing[OFF][/red]"

        # Keep the System panel intentionally compact so it doesn't dominate
        # the single-column layout.
        status_lines = [
            f"Current: {self.current_thread[-8:]} | Roots with schedulers: {len(self.active_schedulers)}",
            "Send: Enter/Ctrl+D | New line: Ctrl+J | Clear: Ctrl+E | Quit: Ctrl+C",
            "Commands: /help  |  Display: /togglePanel chat|children|system",
        ]

        # Show only a couple of recent system log entries, and only their
        # first line (multi-line logs belong in the console output).
        tail_lines: List[str] = []
        try:
            recent = (self._system_log or [])[-2:]
            for msg in recent:
                if not isinstance(msg, str) or not msg:
                    continue
                first = msg.splitlines()[0].rstrip()
                # Indicate truncation if the message had more lines.
                if msg.count('\n'):
                    first = first + " …"
                tail_lines.append(first)
        except Exception:
            tail_lines = []

        self.system_output.set_content("\n".join(status_lines + tail_lines))

        # Children panel: refresh at most once every 2 seconds
        try:
            now = time.time()
            if now - self._last_children_refresh >= 2.0:
                try:
                    subtree_text = self.format_tree(self.current_thread)
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

    def render_group(self) -> Group:
        """Render the panel group for the live display."""
        # Single-column layout, top-to-bottom:
        #   System
        #   Children
        #   Chat Messages
        #   (optional) Approval
        #   Input
        children: List[Any] = []
        if self._panel_visible.get('system', True):
            children.append(self.system_output.render())
        if self._panel_visible.get('children', True):
            children.append(self.children_output.render())
        if self._panel_visible.get('chat', True):
            children.append(self.chat_output.render())

        pending = getattr(self, '_pending_prompt', {}) or {}
        # Only render the approval panel when there is a pending prompt
        # and it has non-empty content. Otherwise, omit it entirely so it
        # visually disappears from the layout.
        if pending and getattr(self.approval_panel, 'content', ''):
            children.append(self.approval_panel.render())

        children.append(self.input_panel.render())
        return Group(*children)

    def log_system(self, msg: str) -> None:
        """Log a message to the system panel."""
        if not hasattr(self, '_system_log'):
            self._system_log = []
        self._system_log.append(msg)

    def console_print_message(self, m: Dict[str, Any]) -> None:
        """Print a single message to the console with rich formatting."""
        def fmt_ts(val: Any) -> str:
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
        ts_str = fmt_ts(m.get('ts'))

        # Best-effort lookup of per-message token stats from the
        # current thread snapshot so we can annotate box titles in the
        # static console view.
        pm_tokens: Dict[str, int] = {"content": 0, "reasoning": 0, "tool_calls": 0, "total": 0}
        try:
            if msg_id:
                th = self.db.get_thread(self.current_thread)
                snap_raw = getattr(th, 'snapshot_json', None) if th else None
                if isinstance(snap_raw, str) and snap_raw:
                    snap = json.loads(snap_raw)
                    ts = snap.get('token_stats') or {}
                    if isinstance(ts, dict):
                        pm = ts.get('per_message') or {}
                        if isinstance(pm, dict) and msg_id in pm:
                            info = pm[msg_id] or {}
                            pm_tokens["content"] = int(info.get('content_tokens') or 0)
                            pm_tokens["reasoning"] = int(info.get('reasoning_tokens') or 0)
                            pm_tokens["tool_calls"] = int(info.get('tool_calls_tokens') or 0)
                            pm_tokens["total"] = int(info.get('total_tokens') or (
                                pm_tokens["content"] + pm_tokens["reasoning"] + pm_tokens["tool_calls"]
                            ))
        except Exception:
            pm_tokens = {"content": 0, "reasoning": 0, "tool_calls": 0, "total": 0}

        def panel(renderable, title: str, border: str):
            # Build a unified title with optional timestamp and msg_id
            parts = [title]
            if ts_str:
                parts.append(f"[dim]{ts_str}[/dim]")
            if msg_id:
                parts.append(f"[dim]{msg_id[-8:]}[/dim]")
            full_title = " | ".join(parts)
            try:
                self.console.print(Panel(renderable, title=full_title, border_style=border, box=self._get_static_box()))
            except Exception:
                # Fallback to plain text if Panel fails for any reason
                self.console.print(f"[{border}]{full_title}[/] {getattr(renderable, 'plain', str(renderable))}")

        if role == 'system':
            title = '[bold blue]System[/bold blue]'
            if isinstance(content, str) and content.lower().startswith('llm error:'):
                title = '[bold red]Error[/bold red]'
                panel(Text(content, no_wrap=False, overflow='fold', style='red'), title, 'red')
                return
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title, 'blue')
            return

        if role == 'user':
            title = '[bold green]User[/bold green]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            # Attach content token count if available
            if pm_tokens["content"]:
                title += f" [dim](tok={pm_tokens['content']})[/dim]"
            panel(Text(content, no_wrap=False, overflow='fold', style='green'), title, 'green')
            return

        if role == 'assistant':
            title = '[bold cyan]Assistant[/bold cyan]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            if pm_tokens["content"]:
                title += f" [dim](tok={pm_tokens['content']})[/dim]"
            # Prefer to show reasoning first if present
            reas = m.get('reasoning') or m.get('reasoning_content')
            if isinstance(reas, str) and reas.strip():
                reason_title = '[bold magenta]Reasoning[/bold magenta]'
                if model_key:
                    reason_title += f" [dim](model: {model_key})[/dim]"
                if pm_tokens["reasoning"]:
                    reason_title += f" [dim](tok={pm_tokens['reasoning']})[/dim]"
                panel(Text(reas, no_wrap=False, overflow='fold'), reason_title, 'magenta')
            if content:
                if looks_markdown(content):
                    panel(Markdown(content), title, 'cyan')
                else:
                    panel(Text(content, no_wrap=False, overflow='fold', style='cyan'), title, 'cyan')
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
                tc_title = 'Tool Calls'
                if pm_tokens["tool_calls"]:
                    tc_title += f" [dim](tok={pm_tokens['tool_calls']})[/dim]"
                self.console.print(Panel(Text("\n".join(lines), no_wrap=False, overflow='fold'), title=tc_title, border_style='yellow', box=self._get_static_box()))
            # Streamed-only metadata if present in snapshot (optional)
            tstream = m.get('tool_stream') or {}
            if isinstance(tstream, dict) and tstream:
                for nm, txt in tstream.items():
                    if txt:
                        self.console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Output: {nm}', border_style='yellow', box=self._get_static_box()))
            tc_stream = m.get('tool_calls_stream') or {}
            if isinstance(tc_stream, dict) and tc_stream:
                for nm, txt in tc_stream.items():
                    if txt:
                        self.console.print(Panel(Text(txt, no_wrap=False, overflow='fold'), title=f'Tool Call Args (streamed): {nm}', border_style='yellow', box=self._get_static_box()))
            return

        if role == 'tool':
            name = m.get('name') or 'Tool'
            title = f'[bold yellow]{name}[/bold yellow]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            # For tool messages, content tokens are the primary signal.
            if pm_tokens["content"]:
                title += f" [dim](tok={pm_tokens['content']})[/dim]"
            panel(Text(content, no_wrap=False, overflow='fold', style='yellow'), title, 'yellow')
            return

        # Fallback generic
        title = (role or 'Message')
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title, 'blue')

    def console_print_block(self, title: str, text: str, border_style: str = 'blue') -> None:
        """Print a titled block to the console."""
        try:
            # Parse rich markup within the text for colored segments
            self.console.print(Panel(Text.from_markup(text), title=title, border_style=border_style, box=self._get_static_box()))
        except Exception:
            # Fallback plain
            self.console.print(f"{title}\n{text}")

    def print_static_view_current(self, heading: Optional[str] = None) -> None:
        """Print a static view of recent messages for the selected thread."""
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
                self.console.print(Panel(heading, border_style='blue', box=self._get_static_box()))
            except Exception:
                self.console.print(heading)
        msgs = snapshot_messages(self.db, tid)
        if not msgs:
            self.console.print(Panel('[dim]No messages yet[/dim]', border_style='blue', box=self._get_static_box()))
        else:
            for m in msgs:
                if isinstance(m, dict):
                    self.console_print_message(m)
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

    def print_banner(self) -> None:
        """Print the static console banner (above the live panels)."""
        try:
            self.console.print("[bold blue]Egg Chat (eggdisplay UI)[/bold blue]")
            self.console.print(
                "Press Enter or Ctrl+D to send (configurable). Ctrl+E clears input. Ctrl+P paste, "
                "Ctrl+C to quit. Type /help for commands.\n"
            )
        except Exception:
            pass

    def redraw_static_view(self, *, reason: str = '') -> None:
        """Clear terminal and reprint static transcript for current thread."""
        try:
            # Clear the terminal so Rich can rewrap content at the new width.
            self.console.clear()
        except Exception:
            pass
        self.print_banner()
        heading = f"Redraw: {reason}\nSwitched to thread: {self.current_thread}" if reason else f"Switched to thread: {self.current_thread}"
        self.print_static_view_current(heading=heading)
