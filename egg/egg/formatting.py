"""Formatting mixin for the egg application."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set


CHILDREN_PANEL_MAXIMAL_LIMIT = 4
CHILDREN_PANEL_COMPACT_LIMIT = 15

from eggthreads import (
    COMPACTION_EVENT_TYPE,
    list_children_with_meta,
    list_root_threads,
    list_threads,
    get_thread_status,
    get_thread_statuses_bulk,
)
from eggthreads.content_parts import content_to_plain_text
from eggthreads.output_optimizer.observability import format_output_optimizer_summary

from .utils import snapshot_messages
from .min_run_summary import (
    MinHiddenActivitySummary,
    count_min_hidden_text_tokens,
    format_min_hidden_activity_summary,
    min_message_token_count,
    serialize_min_tool_call_tokens,
    snapshot_per_message_token_stats,
)


class FormattingMixin:
    """Mixin providing display/formatting methods for EggDisplayApp."""

    def header_cost_metric(self, api_usage: Dict[str, Any]) -> str:
        """Return compact total-cost text for panel headers.

        The caller passes the already-cached ``api_usage`` returned by
        ``current_token_stats()``.  This keeps header rendering cheap: no extra
        token/cost scan is triggered just to show the cost.
        """

        if not isinstance(api_usage, dict):
            return ""
        cost_info = api_usage.get("cost_usd")
        if not isinstance(cost_info, dict):
            return ""
        cost_total = cost_info.get("total")
        try:
            return self._fmt_header_metric(cost_total, 'cost')
        except Exception:
            return ""

    def _snapshot_last_event_seq(self, thread_id: str) -> int:
        """Return the thread snapshot watermark without loading snapshot JSON."""
        try:
            cur = self.db.conn.execute(
                "SELECT snapshot_last_event_seq FROM threads WHERE thread_id=?",
                (thread_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else -1
        except Exception:
            return -1

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

        # Get roots with live schedulers from self.
        scheduled_set: Set[str] = set()
        try:
            from eggthreads.runner import scheduler_task_is_live

            active = getattr(self, 'active_schedulers', {}) or {}
            scheduled_set = {
                rid for rid, entry in active.items()
                if isinstance(entry, dict) and scheduler_task_is_live(entry.get('task'))
            }
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

        # Find roots. Runtime threads should be real children of the thread
        # that started the REPL tool call, but legacy unparented rows still
        # need to remain visible/inspectable rather than being hidden.
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

    def format_children_panel(self, root_tid: str) -> str:
        """Format the selected thread subtree at a density suited to the panel.

        The selected thread is the view root, not one of its descendants. A
        recursive aggregate chooses the density without materializing the
        subtree. Only compact mode fetches descendant IDs; minimal mode remains
        constant-size in Python. ``format_tree`` intentionally remains the
        maximal, inspectable view used by commands such as ``/listChildren``.
        """
        from rich.markup import escape as rich_escape

        cur = self.db.conn.execute(
            """
            WITH RECURSIVE descendants(thread_id) AS (
                SELECT child_id
                FROM children
                WHERE parent_id=? AND child_id<>?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN descendants d ON c.parent_id=d.thread_id
                WHERE c.child_id<>?
            )
            SELECT
                COUNT(DISTINCT d.thread_id),
                COUNT(DISTINCT CASE
                    WHEN o.lease_until > datetime('now') THEN d.thread_id
                END)
            FROM descendants d
            LEFT JOIN open_streams o ON o.thread_id=d.thread_id
            """,
            (root_tid, root_tid, root_tid),
        )
        row = cur.fetchone()
        descendant_count = int(row[0] or 0) if row else 0
        streaming_count = int(row[1] or 0) if row else 0

        if descendant_count <= CHILDREN_PANEL_MAXIMAL_LIMIT:
            return self.format_tree(root_tid)
        if descendant_count > CHILDREN_PANEL_COMPACT_LIMIT:
            return (
                f"{descendant_count} descendants · "
                f"{streaming_count} streaming"
            )

        cur = self.db.conn.execute(
            """
            WITH RECURSIVE descendants(thread_id) AS (
                SELECT child_id
                FROM children
                WHERE parent_id=? AND child_id<>?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN descendants d ON c.parent_id=d.thread_id
                WHERE c.child_id<>?
            )
            SELECT thread_id FROM descendants ORDER BY thread_id
            """,
            (root_tid, root_tid, root_tid),
        )
        descendant_ids = [str(row[0]) for row in cur.fetchall() if row[0]]
        status_map = get_thread_statuses_bulk(
            self.db, descendant_ids, skip_runnability=True
        )
        streaming_ids = [
            tid for tid in descendant_ids if status_map.get(tid) == 'streaming'
        ]
        streaming_set = set(streaming_ids)
        not_streaming_ids = [
            tid for tid in descendant_ids if tid not in streaming_set
        ]

        def suffixes(thread_ids: List[str]) -> str:
            return ", ".join(rich_escape(tid[-8:]) for tid in thread_ids) or "none"

        return "\n".join((
            f"{descendant_count} descendants",
            f"[bold yellow]Streaming ({len(streaming_ids)}):[/] {suffixes(streaming_ids)}",
            f"[dim]Not streaming ({len(not_streaming_ids)}):[/] {suffixes(not_streaming_ids)}",
        ))

    def _compaction_marker_text(self, marker: Dict[str, Any]) -> str:
        """Return the textual transcript divider for a compaction event."""
        start_msg_id = str(marker.get('start_msg_id') or '')
        start_event_seq = marker.get('start_event_seq')
        selector = str(marker.get('selector') or '')
        created_by = str(marker.get('created_by') or '')
        event_seq = marker.get('event_seq')

        start_short = start_msg_id[-8:] if start_msg_id else 'unknown'
        details: List[str] = []
        if event_seq is not None:
            details.append(f"marker #{event_seq}")
        if start_event_seq is not None:
            details.append(f"start event #{start_event_seq}")
        if selector:
            details.append(f"selector {selector}")
        if created_by:
            details.append(f"by {created_by}")
        detail_text = f" ({'; '.join(details)})" if details else ""
        return (
            "────────────────────────────────────────────────────────\n"
            f"Compaction boundary: API context now starts at msg_{start_short}{detail_text}.\n"
            "Earlier messages remain visible in the UI/raw history.\n"
            "────────────────────────────────────────────────────────"
        )

    def _compaction_markers_by_start_seq(self, thread_id: str) -> Dict[int, List[Dict[str, Any]]]:
        """Return raw compaction markers keyed by their start message event sequence."""
        try:
            cur = self.db.conn.execute(
                "SELECT event_seq, ts, payload_json FROM events "
                "WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
                (thread_id, COMPACTION_EVENT_TYPE),
            )
        except Exception:
            return {}

        markers: Dict[int, List[Dict[str, Any]]] = {}
        for row in cur.fetchall():
            try:
                payload = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else (row['payload_json'] or {})
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            try:
                start_seq = int(payload.get('start_event_seq'))
            except Exception:
                continue
            marker = dict(payload)
            try:
                marker['event_seq'] = int(row['event_seq'])
            except Exception:
                marker['event_seq'] = row['event_seq']
            marker['ts'] = row['ts']
            markers.setdefault(start_seq, []).append(marker)
        return markers

    def _display_verbosity_level(self) -> str:
        """Return the current terminal display verbosity level."""
        level = str(getattr(self, '_display_verbosity', 'max') or 'max').strip().lower()
        return level if level in {'max', 'medium', 'min'} else 'max'

    def _one_line_display_preview(self, text: Any, *, max_chars: int = 160) -> str:
        """Return a compact one-line preview for collapsed display rows."""
        preview = " ".join(str(text or '').split())
        if max_chars > 3 and len(preview) > max_chars:
            return preview[: max_chars - 3].rstrip() + "..."
        return preview

    def _output_optimizer_summary(self, message: Dict[str, Any], *, include_artifact_id: bool = False) -> str:
        """Return compact optimizer metadata for display, if present."""

        metadata = message.get('output_optimizer') if isinstance(message, dict) else None
        if not isinstance(metadata, dict):
            return ''
        try:
            return format_output_optimizer_summary(metadata, include_artifact_id=include_artifact_id)
        except Exception:
            return ''

    def format_messages_text(self, thread_id: str, messages: Optional[List[Dict[str, Any]]] = None) -> str:
        """Format messages in a thread for display."""
        msgs = messages if messages is not None else snapshot_messages(self.db, thread_id)
        markers_by_start_seq = self._compaction_markers_by_start_seq(thread_id)
        lines: List[str] = []
        if not msgs and not markers_by_start_seq:
            return "No messages yet."

        verbosity = self._display_verbosity_level()
        per_message_tokens = snapshot_per_message_token_stats(self.db, thread_id) if verbosity == 'min' and messages is None else {}
        if verbosity == 'min' and not per_message_tokens and messages is not None:
            try:
                from eggthreads import snapshot_token_stats

                token_stats = snapshot_token_stats({'messages': [m for m in msgs if isinstance(m, dict)]})
                pm = token_stats.get('per_message') if isinstance(token_stats, dict) else {}
                if isinstance(pm, dict):
                    per_message_tokens = {str(k): v for k, v in pm.items() if isinstance(v, dict)}
            except Exception:
                per_message_tokens = {}
        hidden_summary = MinHiddenActivitySummary()

        def add_hidden_reasoning(*, tokens: Any = 0) -> None:
            hidden_summary.add_reasoning_block(tokens=tokens)

        def add_hidden_tool_call(*, name: Any = None, tokens: Any = 0, tool_call_id: str = "") -> None:
            hidden_summary.add_tool_execution(name=name, tokens=tokens, tool_call_id=tool_call_id)

        def add_hidden_tool_result(*, name: Any = None, tokens: Any = 0) -> None:
            hidden_summary.add_tool_result(name=name, tokens=tokens)

        def flush_hidden() -> None:
            if verbosity != 'min' or not hidden_summary.has_activity():
                return
            summary = format_min_hidden_activity_summary(hidden_summary)
            if summary:
                lines.append(summary)
            hidden_summary.clear()

        def tool_call_info(tc: Any) -> tuple[str, str, str]:
            data = tc if isinstance(tc, dict) else {}
            f = data.get('function') if isinstance(data.get('function'), dict) else {}
            name = f.get('name') or data.get('name') or 'function'
            args = f.get('arguments') if 'arguments' in f else data.get('arguments')
            try:
                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or '')
            except Exception:
                args_str = str(args or '')
            tc_id = str(data.get('id') or data.get('tool_call_id') or '')
            return str(name or 'function'), args_str, tc_id

        def min_system_message_is_visible(content: str) -> bool:
            # All system messages are visible in min verbosity.
            return True

        emitted_marker_keys: Set[tuple[int, int]] = set()
        for m in msgs:
            msg_id = str(m.get('msg_id') or '')
            msg_id_text = f" [msg_id: {msg_id}]" if msg_id else ""
            try:
                event_seq_int = int(m.get('event_seq'))
            except Exception:
                event_seq_int = -1
            for marker in markers_by_start_seq.get(event_seq_int, []):
                key = (event_seq_int, int(marker.get('event_seq') or -1))
                if key not in emitted_marker_keys:
                    if verbosity == 'min':
                        flush_hidden()
                    lines.append(self._compaction_marker_text(marker))
                    emitted_marker_keys.add(key)

            role = m.get('role')
            tps_text = ""
            try:
                tps_val = m.get('tps')
                if isinstance(tps_val, (int, float)) and tps_val > 0:
                    tps_text = f" ({self._fmt_header_metric(tps_val, 'tps')})"
            except Exception:
                tps_text = ""
            if role == 'assistant':
                is_assistant_note = bool(m.get('answer_user_preserve_turn'))
                reas = (m.get('reasoning') or m.get('reasoning_content') or '').strip()
                if reas and not is_assistant_note:
                    reason_header = f"[Reasoning{tps_text}{msg_id_text}]"
                    if verbosity == 'max':
                        lines.append(f"{reason_header}\n{reas}")
                    elif verbosity == 'medium':
                        lines.append(reason_header)
                    else:
                        add_hidden_reasoning(tokens=min_message_token_count(per_message_tokens, msg_id, 'reasoning', reas))
                content = content_to_plain_text(m.get('content')).strip()
                if content:
                    if verbosity == 'min':
                        flush_hidden()
                    header = "Assistant Note" if is_assistant_note else "Assistant"
                    lines.append(f"[{header}{tps_text}{msg_id_text}]\n{content}")
                # Final tool calls summary (if any)
                tcs = m.get('tool_calls') or []
                if isinstance(tcs, list) and tcs:
                    if verbosity == 'max':
                        for tc in tcs:
                            f = (tc or {}).get('function') or {}
                            name = f.get('name') or ''
                            args = f.get('arguments')
                            try:
                                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or '')
                            except Exception:
                                args_str = str(args or '')
                            lines.append(f"[ToolCall] {name} {args_str}")
                    elif verbosity == 'medium':
                        tc_lines: List[str] = []
                        for tc in tcs:
                            name, args_str, tc_id = tool_call_info(tc)
                            tc_id_text = f" [tool_call_id: {tc_id}]" if tc_id else ""
                            preview = self._one_line_display_preview(args_str)
                            suffix = f" {preview}" if preview else ""
                            tc_lines.append(f"[ToolCall{tc_id_text}] {name}{suffix}")
                        if tc_lines:
                            lines.append(f"[Tool Calls{tps_text}{msg_id_text}]\n" + "\n".join(tc_lines))
                    else:
                        tool_call_tokens = min_message_token_count(
                            per_message_tokens,
                            msg_id,
                            'tool_calls',
                            serialize_min_tool_call_tokens(tcs),
                        )
                        for idx, tc in enumerate(tcs):
                            name, _args_str, tc_id = tool_call_info(tc)
                            add_hidden_tool_call(
                                name=name,
                                tokens=tool_call_tokens if idx == 0 else 0,
                                tool_call_id=tc_id,
                            )
                # Streamed-only metadata (if snapshot captured)
                tstream = m.get('tool_stream') or {}
                if isinstance(tstream, dict):
                    for nm, txt in tstream.items():
                        if txt:
                            header = f"[Tool Output: {nm}{tps_text}{msg_id_text}]" if verbosity != 'max' else f"[Tool Output: {nm}]"
                            if verbosity == 'max':
                                lines.append(f"{header}\n{txt}")
                            elif verbosity == 'medium':
                                lines.append(header)
                            else:
                                add_hidden_tool_result(name=nm, tokens=count_min_hidden_text_tokens(txt))
                tc_stream = m.get('tool_calls_stream') or {}
                if isinstance(tc_stream, dict):
                    for nm, txt in tc_stream.items():
                        if txt:
                            header = f"[Tool Call Args: {nm}{tps_text}{msg_id_text}]" if verbosity != 'max' else f"[Tool Call Args: {nm}]"
                            if verbosity == 'max':
                                lines.append(f"{header}\n{txt}")
                            elif verbosity == 'medium':
                                preview = self._one_line_display_preview(txt)
                                lines.append(f"{header} {preview}" if preview else header)
                            else:
                                add_hidden_tool_call(
                                    tokens=count_min_hidden_text_tokens(txt),
                                    tool_call_id=str(nm or ''),
                                )
            elif role == 'user':
                content = content_to_plain_text(m.get('content')).strip()
                if content:
                    if verbosity == 'min':
                        flush_hidden()
                    lines.append(f"[User{msg_id_text}]\n{content}")
            elif role == 'tool':
                # Distinguish between genuine assistant tool outputs and
                # user-initiated command outputs that are stored as
                # role="tool" with user_tool_call flag.
                if m.get('user_tool_call'):
                    name = m.get('name') or 'user_command'
                    lower_label = 'User Tool'
                else:
                    name = m.get('name') or 'tool'
                    lower_label = 'Tool'
                content = content_to_plain_text(m.get('content')).strip()
                if content:
                    optimizer_summary = self._output_optimizer_summary(m, include_artifact_id=True)
                    if verbosity == 'max':
                        header = f"[Tool: {name}{tps_text}{msg_id_text}]"
                        if optimizer_summary:
                            header += f" [{optimizer_summary}]"
                        lines.append(f"{header}\n{content}")
                    else:
                        tool_call_id = str(m.get('tool_call_id') or '')
                        tool_call_id_text = f" [tool_call_id: {tool_call_id}]" if tool_call_id else ""
                        header = f"[{lower_label}: {name}{tps_text}{msg_id_text}{tool_call_id_text}]"
                        if optimizer_summary:
                            header += f" [{optimizer_summary}]"
                        if verbosity == 'medium':
                            lines.append(header)
                        else:
                            add_hidden_tool_result(
                                name=name,
                                tokens=min_message_token_count(per_message_tokens, msg_id, 'content', content),
                            )
            elif role == 'system':
                content = content_to_plain_text(m.get('content')).strip()
                if content:
                    if verbosity == 'min' and not min_system_message_is_visible(content):
                        continue
                    if verbosity == 'min':
                        flush_hidden()
                    label = 'Continue Status' if m.get('recovery_notice') else 'System'
                    lines.append(f"[{label}{msg_id_text}]\n{content}")
        flush_hidden()
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

    def rebuild_chat_cache_for_current(self, snapshot_seq: Optional[int] = None) -> None:
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
                "display_verbosity": None,
                "base_full": "",
                "base_tail": "",
            }

        snap_seq = snapshot_seq if snapshot_seq is not None else self._snapshot_last_event_seq(self.current_thread)

        display_verbosity = self._display_verbosity_level()
        if (
            self._chat_cache.get("thread_id") == self.current_thread
            and self._chat_cache.get("snapshot_seq") == snap_seq
            and self._chat_cache.get("display_verbosity") == display_verbosity
        ):
            return

        base_full = self.format_messages_text(self.current_thread)
        base_tail = self.truncate_for_chat_panel(base_full)
        self._chat_cache = {
            "thread_id": self.current_thread,
            "snapshot_seq": snap_seq,
            "display_verbosity": display_verbosity,
            "base_full": base_full,
            "base_tail": base_tail,
        }

    def current_token_stats(self, snapshot_seq: Optional[int] = None) -> tuple[Optional[int], Dict[str, Any]]:
        """Return (provider context_tokens, api_usage) for the current thread.

        Uses eggthreads' thread_token_stats so that context_tokens reflects
        the current provider/API context after compaction while api_usage (and
        cost, when configured) updates during streaming instead of only
        after snapshots are rebuilt.

        On any error or when not available, (None, {}) is returned.
        """
        now = time.monotonic()
        active_invoke = ""
        try:
            ls = getattr(self, '_live_state', {}) or {}
            if ls.get('active_invoke'):
                active_invoke = str(ls.get('active_invoke') or '')
        except Exception:
            active_invoke = ""
        cache = getattr(self, '_token_stats_cache', None)
        if snapshot_seq is None:
            try:
                snapshot_seq = self._snapshot_last_event_seq(self.current_thread)
            except Exception:
                snapshot_seq = -1
        if active_invoke and isinstance(cache, dict):
            # During any active stream (LLM or tool), prefer stale token stats
            # for the current thread/snapshot.  Token counts are advisory in
            # the header, and rescanning large histories while the user is
            # typing/scrolling causes visible TUI lag.  The cache refreshes
            # when the snapshot/current thread changes or when the stream ends
            # and the idle cache key is used again.
            key = cache.get('key')
            if (
                isinstance(key, tuple)
                and len(key) >= 2
                and key[0] == self.current_thread
                and key[1] == snapshot_seq
            ):
                return cache.get('value', (None, {}))
        try:
            if active_invoke:
                max_event_seq = snapshot_seq
            else:
                # When idle, ``thread_token_stats()`` is driven by the cached
                # snapshot plus rare post-snapshot message/control events. Use
                # the snapshot sequence as the stable key so unrelated events
                # (for example model/config/tool approval changes) do not
                # force token-stat rescans every tick.
                max_event_seq = snapshot_seq
        except Exception:
            max_event_seq = -1

        cache_key = (self.current_thread, snapshot_seq, max_event_seq, active_invoke)
        if isinstance(cache, dict) and cache.get('key') == cache_key:
            # Idle stats are keyed by the snapshot watermark, so they remain
            # valid until the thread's snapshot changes. Active streams still
            # use a short TTL because their key only tracks the latest event
            # sequence, and live headers should stay responsive.
            ttl = 0.5 if active_invoke else None
            try:
                if ttl is None or (now - float(cache.get('at') or 0.0)) < ttl:
                    return cache.get('value', (None, {}))
            except Exception:
                if ttl is None:
                    return cache.get('value', (None, {}))

        ctx_tokens: Optional[int] = None
        api_usage: Dict[str, Any] = {}
        try:
            from eggthreads import thread_token_stats

            ts = thread_token_stats(self.db, self.current_thread, llm=self.llm_client)
            if isinstance(ts, dict):
                ct = ts.get('context_tokens')
                if isinstance(ct, int):
                    ctx_tokens = ct
                au = ts.get('api_usage')
                if isinstance(au, dict):
                    api_usage = dict(au)
                    ft = ts.get('full_thread_tokens')
                    if isinstance(ft, int):
                        api_usage['full_thread_tokens'] = ft
        except Exception:
            ctx_tokens = None
            api_usage = {}
        self._token_stats_cache = {
            'key': cache_key,
            'active_key': (self.current_thread, snapshot_seq, active_invoke) if active_invoke else None,
            'at': now,
            'value': (ctx_tokens, api_usage),
        }
        return ctx_tokens, api_usage

    def compose_chat_panel_text(self, snapshot_seq: Optional[int] = None) -> str:
        """Compose the text for the Chat Messages panel.

        Snapshots are maintained in various places so we avoid rebuilding here
        and just read whatever snapshot is present. This keeps the chat panel
        up to date while eliminating a large amount of idle CPU work.
        """
        # Ensure cache is up to date for the current thread / snapshot.
        self.rebuild_chat_cache_for_current(snapshot_seq=snapshot_seq)
        base = self._chat_cache.get("base_tail", "No messages yet.")

        # Load approximate token statistics for the header.
        ctx_tokens, api_usage = self.current_token_stats(snapshot_seq=snapshot_seq)
        ls = self._live_state
        parts: List[str] = [base]
        if ls.get('active_invoke'):
            live_tps = self.current_stream_tps()
            live_tps_text = f" ({live_tps})" if live_tps else ""
            provider_duration = ""
            try:
                provider_duration = self._current_provider_stream_duration()
            except Exception:
                provider_duration = ""
            if provider_duration:
                parts.append(f"\n[Provider status]\n{provider_duration}")
            if ls.get('reason'):
                parts.append(f"\n[Reasoning (streaming){live_tps_text}]\n{ls['reason']}")
            reasoning_summary = ls.get('reasoning_summary') or {}
            if isinstance(reasoning_summary, dict) and reasoning_summary.get('active') and reasoning_summary.get('text'):
                parts.append(
                    f"\n[Reasoning Summary (streaming){live_tps_text}]\n"
                    f"{reasoning_summary.get('text')}"
                )
            for pk in ls.get('tc_order') or []:
                delta = (ls.get('tc_text') or {}).get(pk, '')
                if delta:
                    label = (ls.get('tc_names') or {}).get(pk) or pk
                    parts.append(f"\n[Tool Call Args: {label}]\n{delta}")
            for name, txt in (ls.get('tools') or {}).items():
                if txt:
                    parts.append(f"\n[Tool: {name} (streaming)]\n{txt}")
            countdown = ""
            try:
                countdown = self._current_tool_timeout_countdown()
            except Exception:
                countdown = ""
            if countdown:
                parts.append(f"\n[Tool status]\n{countdown}")
            summary = ls.get('tool_summary') or {}
            if isinstance(summary, dict) and summary.get('active') and summary.get('text'):
                name = str(summary.get('name') or 'tool')
                parts.append(f"\n[Tool: {name} status]\n{summary.get('text')}")
            indicator = ls.get('tool_stream_indicator') or {}
            if isinstance(indicator, dict) and indicator.get('active'):
                name = str(indicator.get('name') or 'tool')
                try:
                    text = self._tool_stream_indicator_text(
                        name=name,
                        frames=int(indicator.get('frames') or 0),
                    )
                except Exception:
                    text = "preview limit reached; saving output only"
                parts.append(
                    f"\n[Tool: {name} (streaming)]\n"
                    f"{text}"
                )
            if ls.get('content'):
                parts.append(f"\n[Assistant (streaming){live_tps_text}]\n{ls['content']}")

        # Build header with model and approximate token usage.
        head_parts: List[str] = []
        head_parts.append(f"Thread {self.current_thread[-8:]} | Model: {self.current_model_for_thread(self.current_thread) or 'default'}")

        def fmt_tok(v: int) -> str:
            return self._fmt_compact_count(v)

        if isinstance(ctx_tokens, int):
            tok_text = fmt_tok(ctx_tokens)
            if tok_text:
                head_parts.append(f"ctx {tok_text}")

        if isinstance(api_usage, dict) and api_usage:
            ti = api_usage.get("total_input_tokens")
            to = api_usage.get("total_output_tokens")
            cc = api_usage.get("approx_call_count")
            cost_text = self.header_cost_metric(api_usage)
            pieces: List[str] = []
            if isinstance(ti, int):
                tok_text = fmt_tok(ti)
                if tok_text:
                    pieces.append(f"in {tok_text}")
            if isinstance(to, int):
                tok_text = fmt_tok(to)
                if tok_text:
                    pieces.append(f"out {tok_text}")
            if isinstance(cc, int):
                calls_text = self._fmt_header_metric(cc, 'calls')
                if calls_text:
                    pieces.append(calls_text)
            if cost_text:
                pieces.append(cost_text)
            if pieces:
                head_parts.append(" ".join(pieces))

        head = "  |  ".join(head_parts)

        # Combine historical + streaming text.
        body = "\n".join(parts).strip() or "No messages yet."

        return head + "\n" + body
