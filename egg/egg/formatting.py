"""Formatting mixin for the egg application."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

from eggthreads import list_children_with_meta, list_root_threads, list_threads, get_thread_status, get_thread_statuses_bulk

from .utils import snapshot_messages


class FormattingMixin:
    """Mixin providing display/formatting methods for EggDisplayApp."""

    def format_thread_line(self, tid: str) -> str:
        """Format a single thread line for display."""
        from rich.markup import escape as rich_escape

        th = self.db.get_thread(tid)
        # Use real-time status from get_thread_status (checks lease expiration properly)
        status = get_thread_status(self.db, tid)
        # Escape user content to prevent Rich markup interference
        recap = rich_escape((th.short_recap if th and th.short_recap else 'No recap').strip())
        mk = rich_escape(self.current_model_for_thread(tid) or 'default')
        label = rich_escape(th.name if th and th.name else '')
        id_short = tid[-8:]

        # Status-based flags
        sflag = '[bold yellow]STREAMING[/bold yellow] ' if status == 'streaming' else ''
        cur_tag = '[bold cyan][CUR][/bold cyan] ' if tid == self.current_thread else ''
        sched_tag = '[bold cyan][SCHED][/bold cyan] ' if self.is_thread_scheduled(tid) else ''

        # Color status
        if status == 'streaming':
            status_tag = f"[bold yellow]{status}[/]"
        elif status == 'runnable':
            status_tag = f"[bold green]{status}[/]"
        else:
            status_tag = f"[dim]{status}[/]"
        return (
            f"{cur_tag}{sched_tag}{sflag}[dim]{id_short}[/dim] {status_tag} - {recap} "
            f"[dim][model: {mk}][/dim]" + (f"  [dim]{label}[/dim]" if label else '')
        )

    def format_tree(self, root_tid: Optional[str] = None) -> str:
        """Format a thread tree for display (optimized with bulk queries)."""
        # Fetch all data upfront to avoid N+1 queries
        all_threads = list_threads(self.db)
        if not all_threads:
            return 'No threads.'

        # Build lookup maps
        threads_by_id = {t.thread_id: t for t in all_threads}

        # Fetch all parent-child relationships in one query
        children_map: Dict[str, List[str]] = {}  # parent_id -> [child_ids]
        parent_set: Set[str] = set()  # threads that have a parent
        try:
            cur = self.db.conn.execute("SELECT parent_id, child_id FROM children ORDER BY rowid")
            for row in cur.fetchall():
                parent_id, child_id = row[0], row[1]
                if parent_id not in children_map:
                    children_map[parent_id] = []
                children_map[parent_id].append(child_id)
                parent_set.add(child_id)
        except Exception:
            pass

        # Fetch all model settings in one query
        model_map: Dict[str, str] = {}
        try:
            cur = self.db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
            for row in cur.fetchall():
                model_map[row[0]] = row[1]
        except Exception:
            pass

        # For threads without explicit model, use initial_model_key
        for t in all_threads:
            if t.thread_id not in model_map and t.initial_model_key:
                model_map[t.thread_id] = t.initial_model_key

        # Compute real-time status for all threads in one batch (efficient)
        all_tids = [t.thread_id for t in all_threads]
        status_map = get_thread_statuses_bulk(self.db, all_tids, skip_runnability=True)

        # Get scheduled threads from self
        scheduled_set: Set[str] = set()
        try:
            scheduled_set = set(getattr(self, 'schedulers', {}).keys())
        except Exception:
            pass

        current_thread = getattr(self, 'current_thread', None)

        def format_line_fast(tid: str) -> str:
            """Format thread line using pre-fetched data."""
            from rich.markup import escape as rich_escape

            th = threads_by_id.get(tid)
            if not th:
                return f"[dim]{tid[-8:]}[/dim] (not found)"

            status = status_map.get(tid, 'idle')
            # Escape user content to prevent Rich markup interference
            recap = rich_escape((th.short_recap or 'No recap').strip())
            mk = rich_escape(model_map.get(tid, 'default'))
            label = rich_escape(th.name or '')
            id_short = tid[-8:]

            sflag = '[bold yellow]STREAMING[/bold yellow] ' if status == 'streaming' else ''
            cur_tag = '[bold cyan][CUR][/bold cyan] ' if tid == current_thread else ''
            sched_tag = '[bold cyan][SCHED][/bold cyan] ' if tid in scheduled_set else ''

            # Color status based on real-time state
            if status == 'streaming':
                status_tag = f"[bold yellow]{status}[/]"
            elif status == 'runnable':
                status_tag = f"[bold green]{status}[/]"
            else:
                status_tag = f"[dim]{status}[/]"

            return (
                f"{cur_tag}{sched_tag}{sflag}[dim]{id_short}[/dim] {status_tag} - {recap} "
                f"[dim][model: {mk}][/dim]" + (f"  [dim]{label}[/dim]" if label else '')
            )

        def render_tree(tid: str, prefix: str = '', is_last: bool = True, out: Optional[List[str]] = None):
            if out is None:
                out = []
            connector = '└─ ' if is_last else '├─ '
            indent_next = '   ' if is_last else '│  '
            base_line = format_line_fast(tid)
            out.append(prefix + connector + base_line)

            kids = children_map.get(tid, [])
            for i, cid in enumerate(kids):
                last = (i == len(kids) - 1)
                render_tree(cid, prefix + indent_next, last, out)
            return out

        # Find roots
        if root_tid:
            roots = [root_tid]
        else:
            roots = [t.thread_id for t in all_threads if t.thread_id not in parent_set]

        lines: List[str] = []
        if not roots:
            return 'No threads.'
        for rid in roots:
            lines.extend(render_tree(rid))
        return "\n".join(lines)

    def format_messages_text(self, thread_id: str) -> str:
        """Format all messages in a thread for display."""
        msgs = snapshot_messages(self.db, thread_id)
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

    def format_model_info(self, concrete_model_info, model_key=None):
        """Format concrete model info dict as a human-readable string."""
        if not concrete_model_info or concrete_model_info == {}:
            if model_key:
                return f"Model: {model_key}\nNo concrete configuration available."
            else:
                return "No concrete configuration available."
        # Pretty print the nested dict as JSON with indentation
        result = json.dumps(concrete_model_info, indent=2)
        if model_key:
            return f"Model: {model_key}\n{result}"
        return result

    def truncate_for_chat_panel(self, text: str, max_lines: int = 100) -> str:
        """Return a shortened view of text suitable for the chat panel.

        The static console view printed by print_static_view_current
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

    def rebuild_chat_cache_for_current(self) -> None:
        """Ensure the cached base chat text for the current thread is fresh.

        We key the cache by (thread_id, snapshot_last_event_seq) so that we
        only walk the full snapshot when it actually changes. This eliminates
        a large amount of idle CPU work on long threads, where repeatedly
        calling format_messages_text every 100ms used to be expensive.
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

        base_full = self.format_messages_text(self.current_thread)
        base_tail = self.truncate_for_chat_panel(base_full)
        self._chat_cache = {
            "thread_id": self.current_thread,
            "snapshot_seq": snap_seq,
            "base_full": base_full,
            "base_tail": base_tail,
        }

    def current_token_stats(self) -> tuple[Optional[int], Dict[str, Any]]:
        """Return (context_tokens, api_usage) for the current thread.

        Uses eggthreads' total_token_stats so that token usage (and
        cost, when configured) updates during streaming instead of only
        after snapshots are rebuilt.

        On any error or when not available, (None, {}) is returned.
        """
        ctx_tokens: Optional[int] = None
        api_usage: Dict[str, Any] = {}
        try:
            from eggthreads import total_token_stats

            ts = total_token_stats(self.db, self.current_thread, llm=self.llm_client)
            if isinstance(ts, dict):
                ct = ts.get('context_tokens')
                if isinstance(ct, int):
                    ctx_tokens = ct
                au = ts.get('api_usage')
                if isinstance(au, dict):
                    api_usage = au
        except Exception:
            ctx_tokens = None
            api_usage = {}
        return ctx_tokens, api_usage

    def compose_chat_panel_text(self) -> str:
        """Compose the text for the Chat Messages panel.

        Snapshots are maintained in various places so we avoid rebuilding here
        and just read whatever snapshot is present. This keeps the chat panel
        up to date while eliminating a large amount of idle CPU work.
        """
        # Ensure cache is up to date for the current thread / snapshot.
        self.rebuild_chat_cache_for_current()
        base = self._chat_cache.get("base_tail", "No messages yet.")

        # Load approximate token statistics for the header.
        ctx_tokens, api_usage = self.current_token_stats()
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

        # Build header with model and approximate token usage.
        head_parts: List[str] = []
        head_parts.append(f"Thread {self.current_thread[-8:]} | Model: {self.current_model_for_thread(self.current_thread) or 'default'}")

        def fmt_tok(v: int) -> str:
            # Compact k-style formatting for large numbers, e.g. 1234 -> "1.23k".
            if v < 1000:
                return str(v)
            return f"{v/1000:.2f}k"

        if isinstance(ctx_tokens, int):
            head_parts.append(f"ctx≈{fmt_tok(ctx_tokens)}")

        if isinstance(api_usage, dict) and api_usage:
            ti = api_usage.get("total_input_tokens")
            to = api_usage.get("total_output_tokens")
            cc = api_usage.get("approx_call_count")
            pieces: List[str] = []
            if isinstance(ti, int):
                pieces.append(f"in≈{fmt_tok(ti)}")
            if isinstance(to, int):
                pieces.append(f"out≈{fmt_tok(to)}")
            if isinstance(cc, int):
                pieces.append(f"calls={cc}")
            if pieces:
                head_parts.append(" ".join(pieces))

        head = "  |  ".join(head_parts)

        # Combine historical + streaming text.
        body = "\n".join(parts).strip() or "No messages yet."

        return head + "\n" + body
