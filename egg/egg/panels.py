"""Panel management mixin for the egg application."""
from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from rich.console import Console, Group
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich import box as rich_box

from eggthreads import create_snapshot

from .utils import snapshot_messages, looks_markdown
from .min_run_summary import (
    MinHiddenActivitySummary,
    count_min_hidden_text_tokens,
    format_min_hidden_activity_summary,
    snapshot_per_message_token_stats,
    serialize_min_tool_call_tokens,
)


CHILDREN_PANEL_FALLBACK_REFRESH_SEC = 1.0
CHILDREN_PANEL_RELEVANT_EVENT_TYPES = (
    'msg.create',
    'msg.edit',
    'msg.delete',
    'model.switch',
    'stream.open',
    'stream.close',
    'tool_call.approval',
    'tool_call.output_approval',
)


@dataclass(frozen=True)
class _StaticTranscriptRenderable:
    """Renderable plus plain fallback for static transcript printing."""

    renderable: Any
    fallback: Optional[str] = None
    kind: str = ''


@dataclass(frozen=True)
class _TranscriptScrollbackBlock:
    """One lightweight transcript unit, rendered lazily on demand."""

    kind: str
    payload: Dict[str, Any]


@dataclass
class _TranscriptScrollbackCache:
    """Rendered suffix cache for one terminal width / verbosity pair."""

    next_block_index: int
    rows: List[str]
    complete: bool = False


class TranscriptScrollbackSource:
    """Lazy full-screen scrollback source for the current static transcript.

    The source captures the current thread snapshot plus compaction marker
    events, but it does not render any Rich panels until the renderer asks for
    rows. Rows are rendered from the newest transcript block toward older
    blocks and cached per terminal width and display verbosity.
    """

    def __init__(
        self,
        panels: Any,
        thread_id: Optional[str] = None,
        *,
        refresh_snapshot: bool = True,
    ) -> None:
        self._panels = panels
        self._db = panels.db
        self._thread_id = thread_id or panels.current_thread
        if refresh_snapshot:
            self._refresh_snapshot_if_safe()
        self._blocks = self._load_blocks()
        self._caches: Dict[Tuple[int, str], _TranscriptScrollbackCache] = {}

    def row_count(self, width: int) -> Optional[int]:
        """Return the rendered row count only after this width is complete."""
        cache = self._caches.get(self._cache_key(width))
        if cache is None or not cache.complete:
            return None
        return len(cache.rows)

    def rows_from_bottom(self, width: int, bottom_offset: int, height: int) -> Sequence[str]:
        """Return a bottom-addressed lazy slice of transcript rows."""
        height = max(0, int(height or 0))
        if height <= 0:
            return []
        bottom_offset = max(0, int(bottom_offset or 0))
        cache = self._ensure_rows(width, bottom_offset + height)
        end = len(cache.rows) - bottom_offset
        if end <= 0:
            return []
        start = max(0, end - height)
        return cache.rows[start:end]

    def _refresh_snapshot_if_safe(self) -> None:
        """Mirror static-view snapshot refresh without touching active streams."""
        try:
            row = self._db.current_open(self._thread_id)
        except Exception:
            row = None
        if row is not None:
            return
        try:
            create_snapshot(self._db, self._thread_id)
        except Exception:
            pass

    def _load_blocks(self) -> List[_TranscriptScrollbackBlock]:
        """Read the current snapshot/messages and compaction marker events."""
        try:
            msgs = snapshot_messages(self._db, self._thread_id)
        except Exception:
            msgs = []
        try:
            markers_by_start_seq = self._panels._compaction_markers_by_start_seq(self._thread_id)
        except Exception:
            markers_by_start_seq = {}

        blocks: List[_TranscriptScrollbackBlock] = []
        if not msgs and not markers_by_start_seq:
            blocks.append(_TranscriptScrollbackBlock('empty', {}))
            return blocks

        for msg in msgs or []:
            if not isinstance(msg, dict):
                continue
            try:
                event_seq_int = int(msg.get('event_seq'))
            except Exception:
                event_seq_int = -1
            for marker in markers_by_start_seq.get(event_seq_int, []):
                if isinstance(marker, dict):
                    blocks.append(_TranscriptScrollbackBlock('marker', dict(marker)))
            blocks.append(_TranscriptScrollbackBlock('message', dict(msg)))
        return blocks

    def _cache_key(self, width: int) -> Tuple[int, str]:
        width = max(1, int(width or 0))
        try:
            verbosity = self._panels._panel_display_verbosity_level()
        except Exception:
            verbosity = str(getattr(self._panels, '_display_verbosity', 'max') or 'max')
        verbosity = str(verbosity or 'max').strip().lower()
        if verbosity not in {'max', 'medium', 'min'}:
            verbosity = 'max'
        return width, verbosity

    def _ensure_rows(self, width: int, needed_from_bottom: int) -> _TranscriptScrollbackCache:
        key = self._cache_key(width)
        cache = self._caches.get(key)
        if cache is None:
            cache = _TranscriptScrollbackCache(
                next_block_index=len(self._blocks) - 1,
                rows=[],
            )
            self._caches[key] = cache

        verbosity = key[1]
        shared_hidden_details = None
        if verbosity == 'min':
            try:
                shared_hidden_details = self._panels._new_static_hidden_details_state()
            except Exception:
                shared_hidden_details = None

        def _flush_shared_hidden() -> None:
            nonlocal shared_hidden_details
            if isinstance(shared_hidden_details, dict):
                try:
                    hidden_item = self._panels._static_hidden_details_renderable(shared_hidden_details)
                except Exception:
                    hidden_item = None
                if hidden_item is not None:
                    hidden_rows = self._render_static_transcript_item_rows(hidden_item, key[0])
                    if hidden_rows:
                        cache.rows[:0] = hidden_rows
                # shared_hidden_details was cleared by _static_hidden_details_renderable (consume=True)

        needed_from_bottom = max(0, int(needed_from_bottom or 0))
        while not cache.complete and len(cache.rows) < needed_from_bottom:
            if cache.next_block_index < 0:
                cache.complete = True
                break
            block = self._blocks[cache.next_block_index]
            cache.next_block_index -= 1

            if verbosity != 'min' or self._is_min_block_visible(block):
                # Visible boundary: flush accumulated hidden details first,
                # then render the block without the shared state.
                _flush_shared_hidden()
                block_rows = self._render_block_rows(block, key[0], verbosity, hidden_details=None)
            else:
                # Hidden block: let it accumulate into the shared state.
                block_rows = self._render_block_rows(block, key[0], verbosity, hidden_details=shared_hidden_details)

            if block_rows:
                cache.rows[:0] = block_rows

        # Flush any remaining accumulated hidden details at the end
        _flush_shared_hidden()

        if cache.next_block_index < 0:
            cache.complete = True
        return cache

    @staticmethod
    def _is_min_block_visible(block: _TranscriptScrollbackBlock) -> bool:
        """Return True if *block* produces visible output in min verbosity."""
        if block.kind != 'message':
            return True  # marker, empty are always visible
        m = block.payload
        if not isinstance(m, dict):
            return True
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if role == 'user':
            return True
        if role == 'assistant':
            if m.get('answer_user_preserve_turn'):
                return bool(content)
            return bool(content)
        if role == 'system':
            return True
        # tool messages are hidden in min
        return False

    def _render_block_rows(
        self,
        block: _TranscriptScrollbackBlock,
        width: int,
        verbosity: str,
        *,
        hidden_details: Any = None,
    ) -> List[str]:
        items: List[_StaticTranscriptRenderable] = []
        if block.kind == 'message':
            own_details = None
            if hidden_details is not None:
                own_details = hidden_details
            elif verbosity == 'min':
                try:
                    own_details = self._panels._new_static_hidden_details_state()
                except Exception:
                    own_details = None
            try:
                items.extend(self._panels._static_transcript_message_renderables(
                    block.payload,
                    own_details,
                ))
            except Exception:
                items = []
            if hidden_details is None and verbosity == 'min' and isinstance(own_details, dict):
                try:
                    hidden_item = self._panels._static_hidden_details_renderable(own_details)
                except Exception:
                    hidden_item = None
                if hidden_item is not None:
                    items.append(hidden_item)
        elif block.kind == 'marker':
            try:
                items.append(self._panels._static_transcript_compaction_marker_renderable(block.payload))
            except Exception:
                pass
        elif block.kind == 'empty':
            try:
                renderable = Panel('[dim]No messages yet[/dim]', border_style='blue', box=self._panels._get_static_box())
            except Exception:
                renderable = 'No messages yet'
            items.append(_StaticTranscriptRenderable(renderable, 'No messages yet'))

        rows: List[str] = []
        for item in items:
            rows.extend(self._render_static_transcript_item_rows(item, width))
        return rows

    def _render_static_transcript_item_rows(
        self,
        item: _StaticTranscriptRenderable,
        width: int,
    ) -> List[str]:
        """Render one transcript item to sanitized ANSI terminal rows."""
        width = max(1, int(width or 0))
        try:
            color_system = self._panels.console.color_system
        except Exception:
            color_system = 'truecolor'

        fallback = item.fallback
        if fallback is None:
            fallback = getattr(item.renderable, 'plain', str(item.renderable))
        for renderable in (item.renderable, fallback):
            try:
                buf = io.StringIO()
                console = Console(
                    file=buf,
                    width=width,
                    force_terminal=True,
                    color_system=color_system or 'truecolor',
                )
                console.print(renderable)
                text = self._sanitize_rendered_ansi(buf.getvalue())
                lines = text.split('\n')
                if lines and lines[-1] == '':
                    lines.pop()
                return lines
            except Exception:
                continue
        return []

    @staticmethod
    def _sanitize_rendered_ansi(text: str) -> str:
        try:
            from eggdisplay import FullScreenDiffRenderer  # type: ignore

            return FullScreenDiffRenderer._sanitize_rendered_ansi(text)
        except Exception:
            return text


class PanelsMixin:
    """Mixin providing panel management methods for EggDisplayApp."""

    def _fmt_compact_count(self, value: Any) -> str:
        """Format a compact positive integer count without a unit suffix."""
        try:
            iv = int(value)
        except Exception:
            return ""
        if iv <= 0:
            return ""
        return f"{iv}" if iv < 1000 else f"{iv/1000:.2f}k"

    def _fmt_header_metric(self, value: Any, label: str) -> str:
        """Format a compact header metric as '<value> <label>'."""
        if label == 'tok':
            compact = self._fmt_compact_count(value)
            return f"{compact} tok" if compact else ""
        if label == 'tps':
            try:
                fv = float(value)
            except Exception:
                return ""
            if fv <= 0:
                return ""
            return f"{fv:.1f} tps" if fv < 10 else f"{fv:.0f} tps"
        if label == 'calls':
            try:
                iv = int(value)
            except Exception:
                return ""
            if iv < 0:
                return ""
            return f"{iv} calls"
        if label == 'cost':
            try:
                fv = float(value)
            except Exception:
                return ""
            if fv <= 0:
                return ""
            return f"${fv:.4f} cost"
        return ""

    def _live_llm_tps_cached(self, invoke: str) -> Optional[float]:
        """Return live LLM TPS with a short cache to avoid O(deltas) scans per UI tick."""
        if not invoke:
            return None
        now = time.monotonic()
        cache = getattr(self, '_live_tps_cache', None)
        if isinstance(cache, dict) and cache.get('invoke') == invoke:
            try:
                if (now - float(cache.get('at') or 0.0)) < 0.5:
                    return cache.get('value')
            except Exception:
                pass
        try:
            from eggthreads import live_llm_tps_for_invoke
            tps = live_llm_tps_for_invoke(self.db, str(invoke))
        except Exception:
            tps = None
        self._live_tps_cache = {'invoke': invoke, 'at': now, 'value': tps}
        return tps

    def _live_print(self, *args, **kwargs) -> None:
        """Print to console, routing through DiffRenderer when the live loop is active."""
        renderer = getattr(self, '_renderer', None)
        if renderer is not None:
            renderer.print_above(*args, **kwargs)
        else:
            self.console.print(*args, **kwargs)

    def _new_transcript_scrollback_source(self) -> TranscriptScrollbackSource:
        """Create a fresh lazy transcript source for the current thread."""
        return TranscriptScrollbackSource(self)

    def _is_full_screen_scrollback_renderer(self, renderer: Any = None) -> bool:
        """Return True when *renderer* is the active full-screen history surface."""
        if renderer is None:
            renderer = getattr(self, '_renderer', None)
        return (
            renderer is not None
            and not bool(getattr(self, '_display_is_inline', False))
            and hasattr(renderer, 'set_scrollback_source')
        )

    def _mark_static_transcript_printed(self, thread_id: Optional[str] = None) -> None:
        """Record the latest transcript event already represented in static history."""
        tid = thread_id or self.current_thread
        try:
            row = self.db.conn.execute(
                "SELECT MAX(event_seq) FROM events WHERE thread_id=? AND type IN ('msg.create', 'thread.compaction')",
                (tid,)
            ).fetchone()
            last = int(row[0]) if row and row[0] is not None else -1
            self._last_printed_seq_by_thread[tid] = last
        except Exception:
            self._last_printed_seq_by_thread[tid] = self._last_printed_seq_by_thread.get(tid, -1)

    def _install_transcript_scrollback_source(
        self,
        renderer: Any = None,
        *,
        reset_session_scrollback: bool = False,
        repaint: bool = False,
    ) -> bool:
        """Install a fresh lazy transcript source on a full-screen renderer.

        ``reset_session_scrollback`` drops rows appended with ``print_above`` so
        replacing the source after redraw/thread/verbosity changes does not show
        the same transcript rows twice (once from the source and once from the
        renderer's in-session scrollback model).
        """
        if renderer is None:
            renderer = getattr(self, '_renderer', None)
        if not self._is_full_screen_scrollback_renderer(renderer):
            return False

        try:
            source = self._new_transcript_scrollback_source()
            if reset_session_scrollback:
                if hasattr(renderer, 'scroll_to_bottom'):
                    try:
                        renderer.scroll_to_bottom()
                    except Exception:
                        pass
                if hasattr(renderer, 'clear_scrollback'):
                    try:
                        renderer.clear_scrollback()
                    except Exception:
                        pass
            if hasattr(renderer, 'invalidate'):
                try:
                    renderer.invalidate()
                except Exception:
                    pass
            renderer.set_scrollback_source(source)
            self._reset_static_hidden_details()
            self._mark_static_transcript_printed()
        except Exception:
            return False

        if repaint and hasattr(renderer, 'update'):
            try:
                renderer.update(self.render_group())
            except Exception:
                pass
        return True

    def print_current_thread(self, heading: Optional[str] = None) -> None:
        """Refresh or print the selected thread after thread-switch commands."""
        renderer = getattr(self, '_renderer', None)
        if self._is_full_screen_scrollback_renderer(renderer):
            self._install_transcript_scrollback_source(
                renderer,
                reset_session_scrollback=True,
                repaint=True,
            )
            return
        self.print_static_view_current(heading=heading)

    def _system_status_key(self) -> Any:
        """Cheap key for System panel model/sandbox/autoapproval title state."""
        cur = self.db.conn.execute(
            """
            WITH RECURSIVE ancestors(thread_id) AS (
                SELECT ?
                UNION ALL
                SELECT c.parent_id
                FROM children c
                JOIN ancestors a ON c.child_id = a.thread_id
            )
            SELECT COUNT(*), COALESCE(MAX(event_seq), 0)
            FROM events
            WHERE type IN (?, ?) AND thread_id IN (SELECT thread_id FROM ancestors)
            """,
            (self.current_thread, 'sandbox.config', 'tool_call.approval'),
        )
        cfg_event_count, cfg_event_max_seq = cur.fetchone()
        model_key = ''
        try:
            model_key = self.current_model_for_thread(self.current_thread) or ''
        except Exception:
            model_key = ''
        return (
            self.current_thread,
            model_key,
            int(cfg_event_count or 0),
            int(cfg_event_max_seq or 0),
        )

    def _compute_children_panel_status_key(self) -> Any:
        """Cheap key for Children panel tree/status state."""
        subtree_ids = self._children_panel_subtree_ids()
        cur = self.db.conn.execute(
            """
            WITH RECURSIVE subtree(thread_id) AS (
                SELECT ?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN subtree s ON c.parent_id = s.thread_id
            )
            SELECT COUNT(*), COALESCE(MAX(c.rowid), 0)
            FROM children c
            JOIN subtree s ON c.parent_id = s.thread_id
            """,
            (self.current_thread,),
        )
        child_count, child_max_rowid = cur.fetchone()
        event_version = self._children_panel_event_version(subtree_ids)
        cur = self.db.conn.execute(
            """
            WITH RECURSIVE subtree(thread_id) AS (
                SELECT ?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN subtree s ON c.parent_id = s.thread_id
            )
            SELECT COUNT(*), COALESCE(GROUP_CONCAT(open_key, '|'), '')
            FROM (
                SELECT o.thread_id || ':' || o.invoke_id || ':' || COALESCE(o.purpose, '') AS open_key
                FROM open_streams o
                JOIN subtree s ON o.thread_id = s.thread_id
                WHERE o.lease_until > datetime('now')
                ORDER BY o.thread_id, o.invoke_id
            )
            """,
            (self.current_thread,),
        )
        open_count, open_key = cur.fetchone()
        return (
            self.current_thread,
            int(child_count or 0),
            int(child_max_rowid or 0),
            int(event_version or 0),
            int(open_count or 0),
            str(open_key or ''),
        )

    def _children_panel_subtree_ids(self) -> Tuple[str, ...]:
        """Return current thread plus descendants for Children panel caching."""
        cur = self.db.conn.execute(
            """
            WITH RECURSIVE subtree(thread_id) AS (
                SELECT ?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN subtree s ON c.parent_id = s.thread_id
            )
            SELECT thread_id FROM subtree ORDER BY thread_id
            """,
            (self.current_thread,),
        )
        return tuple(str(row[0]) for row in cur.fetchall() if row[0])

    def _children_panel_event_version(self, subtree_ids: Sequence[str]) -> int:
        """Increment a local version when new relevant subtree events appear."""
        seen = getattr(self, '_children_panel_seen_event_seq_by_thread', None)
        if not isinstance(seen, dict):
            seen = {}
        version = int(getattr(self, '_children_panel_event_version_value', 0) or 0)

        subtree_set = set(subtree_ids)
        for tid in list(seen.keys()):
            if tid not in subtree_set:
                seen.pop(tid, None)

        placeholders = ', '.join('?' for _ in CHILDREN_PANEL_RELEVANT_EVENT_TYPES)
        for tid in subtree_ids:
            try:
                cur = self.db.conn.execute(
                    f"""
                    SELECT COALESCE(MAX(event_seq), -1)
                    FROM events INDEXED BY events_thread_type
                    WHERE thread_id=? AND type IN ({placeholders})
                    """,
                    (tid, *CHILDREN_PANEL_RELEVANT_EVENT_TYPES),
                )
                row = cur.fetchone()
                relevant_max = int(row[0]) if row and row[0] is not None else -1
            except Exception:
                relevant_max = -1
            last_seen = seen.get(tid)
            if last_seen is None:
                seen[tid] = relevant_max
                continue
            try:
                last_seen_int = int(last_seen)
            except Exception:
                last_seen_int = -1
            if relevant_max > last_seen_int:
                version += 1
                seen[tid] = relevant_max

        self._children_panel_seen_event_seq_by_thread = seen
        self._children_panel_event_version_value = version
        return version

    def _mark_children_panel_dirty(self) -> None:
        """Request a Children panel tree refresh on the next safe panel tick."""
        self._children_panel_dirty = True

    def _get_static_box(self) -> Any:
        """Get the box style to use for static console panels.

        Returns MINIMAL when borders are hidden, SQUARE otherwise.
        """
        if getattr(self, '_borders_visible', True):
            return rich_box.SQUARE
        return rich_box.MINIMAL

    def update_panels(self) -> None:
        """Update all UI panels with current state."""
        try:
            input_active = (
                getattr(self.input_panel, '_cached_render', None) is not None
                and bool(self.input_panel.is_dirty())
            )
        except Exception:
            input_active = False

        try:
            snapshot_seq = self._snapshot_last_event_seq(self.current_thread)
        except Exception:
            snapshot_seq = -1

        # In inline mode the Chat Messages panel body mirrors the
        # conversation + streaming (HEAD behaviour). In full-screen
        # mode the same content lives in the DiffRenderer's static
        # window above the live region so the panel body stays empty
        # (title-only metrics bar).
        if getattr(self, "_display_is_inline", False):
            self.chat_output.set_content(self.compose_chat_panel_text(snapshot_seq=snapshot_seq))
        else:
            self.chat_output.set_content("")

        try:
            chat_header_tps = self.current_chat_header_tps(snapshot_seq=snapshot_seq)
        except Exception:
            chat_header_tps = ""

        ctx_tokens: Optional[int] = None
        try:
            ctx_tokens, _api_usage = self.current_token_stats(snapshot_seq=snapshot_seq)

            def fmt_tok(v: int) -> str:
                return self._fmt_compact_count(v)

            # If we have no token stats yet for this thread, keep the
            # existing title so that we do not clear previously
            # computed information while a new turn is streaming.
            have_ctx = isinstance(ctx_tokens, int)
            if not have_ctx:
                pass  # leave title unchanged
            else:
                title_parts: List[str] = ["Chat Messages"]
                if have_ctx:
                    tok_text = fmt_tok(int(ctx_tokens))
                    if tok_text:
                        title_parts.append(f"ctx {tok_text}")

                if chat_header_tps:
                    title_parts.append(chat_header_tps)
                self.chat_output.title = "  |  ".join(title_parts)
        except Exception:
            # Leave existing title unchanged on any error.
            pass

        metric_parts: List[str] = []
        if not getattr(self, "_display_is_inline", False):
            try:
                if isinstance(ctx_tokens, int):
                    tok_text = self._fmt_compact_count(int(ctx_tokens))
                    if tok_text:
                        metric_parts.append(f"ctx {tok_text}")
            except Exception:
                pass
            if chat_header_tps:
                metric_parts.append(chat_header_tps)

        system_status_key = None
        try:
            system_status_key = self._system_status_key()
        except Exception:
            system_status_key = None

        cache = getattr(self, '_system_status_cache', None)
        if system_status_key is not None and isinstance(cache, dict) and cache.get('key') == system_status_key:
            model_part = str(cache.get('model_part') or r"[cyan]Model\[default][/cyan]")
            sandbox_part = str(cache.get('sandbox_part') or "[red]Sandboxing[OFF][/red]")
            auto_part = str(cache.get('auto_part') or "[green]Autoapproval[Off][/green]")
        else:
            try:
                model_name = self.current_model_for_thread(self.current_thread) or 'default'
            except Exception:
                model_name = 'default'
            model_part = f"[cyan]Model\\[{rich_escape(str(model_name))}][/cyan]"

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
                sandbox_part = f"[green]Sandboxing[{display}][/green]"
            else:
                sandbox_part = "[red]Sandboxing[OFF][/red]"

            try:
                from eggthreads import get_thread_auto_approval_status
                auto_approval = bool(get_thread_auto_approval_status(self.db, self.current_thread))
            except Exception:
                auto_approval = False
            auto_part = "[red]Autoapproval[On][/red]" if auto_approval else "[green]Autoapproval[Off][/green]"
            if system_status_key is not None:
                self._system_status_cache = {
                    'key': system_status_key,
                    'model_part': model_part,
                    'sandbox_part': sandbox_part,
                    'auto_part': auto_part,
                }
        # NO_API_CALLS read-only mode: shown in the header only when active
        # (following the Streaming-indicator convention — no clutter when off).
        try:
            import os as _os
            no_api = _os.environ.get('NO_API_CALLS', '').strip().lower() in ('1', 'true', 'yes')
        except Exception:
            no_api = False
        no_api_part = "[red]ReadOnly\\[NO_API_CALLS][/red]" if no_api else ""
        stream_part = self._current_stream_header_part(include_tps=getattr(self, "_display_is_inline", False))
        title_parts = [model_part]
        if metric_parts:
            title_parts.extend(metric_parts)
        title_parts.extend([sandbox_part, auto_part])
        if no_api_part:
            title_parts.append(no_api_part)
        if stream_part:
            title_parts.append(stream_part)
        title = "  ".join(title_parts)
        self.system_output.title = title

        # System panel is intentionally only the header line: status
        # (sandbox, auto-approval) lives in the title border, body is empty.
        self.system_output.set_content("")

        # Children panel: refresh on watcher/explicit invalidation, and keep a
        # slow fallback check for cross-process or descendant changes. This
        # avoids running the status-key DB probes on every idle UI tick.
        try:
            cached_children_key = getattr(self, '_children_panel_cached_status_key', None)
            cached_children_thread = None
            if isinstance(cached_children_key, tuple) and cached_children_key:
                cached_children_thread = cached_children_key[0]
            children_dirty = (
                bool(getattr(self, '_children_panel_dirty', False))
                or cached_children_key is None
                or cached_children_thread != self.current_thread
            )
            if input_active and cached_children_key is not None:
                # Prefer input echo over refreshing the tree while the user is
                # typing. Background tool leases can make this key change every
                # heartbeat; rebuilding a large tree in that path causes visible
                # multi-second input lag. The next idle tick refreshes it.
                pass
            else:
                now = time.time()
                next_check = float(getattr(self, '_children_panel_next_status_check_at', 0.0) or 0.0)
                if children_dirty or now >= next_check:
                    status_key = self._compute_children_panel_status_key()
                    self._children_panel_next_status_check_at = now + CHILDREN_PANEL_FALLBACK_REFRESH_SEC
                else:
                    status_key = cached_children_key
                if children_dirty or cached_children_key != status_key:
                    try:
                        subtree_text = self.format_tree(self.current_thread)
                    except Exception:
                        subtree_text = "(error rendering children tree)"
                    self.children_output.set_content(subtree_text)
                    self._children_panel_cached_status_key = status_key
                self._children_panel_dirty = False
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

    def _current_stream_header_part(self, *, include_tps: bool = True) -> str:
        """Return a compact live-streaming suffix for the System title.

        Empty when nothing is streaming. For LLM streams, appends live
        TPS when available. For tool streams, appends the active tool
        name (best-effort) so users see what's running.
        """
        ls = getattr(self, '_live_state', {}) or {}
        invoke = ls.get('active_invoke')
        if not invoke:
            return ""
        kind = ls.get('stream_kind') or 'stream'
        if kind == 'llm':
            tps_str = ""
            if include_tps:
                tps = self._live_llm_tps_cached(str(invoke))
                if isinstance(tps, (int, float)) and tps > 0:
                    tps_str = self._fmt_header_metric(tps, 'tps')
            inner = f"llm {tps_str}" if tps_str else "llm"
        elif kind == 'tool':
            tool_name = ""
            summary = ls.get('tool_summary') if isinstance(ls.get('tool_summary'), dict) else {}
            if summary and summary.get('active') and summary.get('text'):
                inner = str(summary.get('text') or '')
                return f"[yellow]Streaming\[{inner}][/yellow]"
            indicator = ls.get('tool_stream_indicator') if isinstance(ls.get('tool_stream_indicator'), dict) else {}
            if indicator and indicator.get('active'):
                tool_name = str(indicator.get('name') or "")
                try:
                    indicator_text = self._tool_stream_indicator_text(
                        name=tool_name,
                        frames=int(indicator.get('frames') or 0),
                        compact=True,
                    )
                except Exception:
                    indicator_text = "tool: saving output"
                inner = indicator_text
                return f"[yellow]Streaming\[{inner}][/yellow]"
            tools = ls.get('tools') if isinstance(ls.get('tools'), dict) else {}
            if tools:
                try:
                    tool_name = next(iter(tools.keys())) or ""
                except Exception:
                    tool_name = ""
            inner = f"tool {tool_name}" if tool_name else "tool"
        else:
            inner = str(kind)
        # Escape inner brackets so Rich doesn't try to parse them as markup.
        return f"[yellow]Streaming\\[{inner}][/yellow]"

    def current_stream_tps(self) -> str:
        """Return a compact live TPS string for the active LLM stream."""
        ls = getattr(self, '_live_state', {}) or {}
        if not ls.get('active_invoke'):
            return ""
        if ls.get('stream_kind') != 'llm':
            return ""
        tps = self._live_llm_tps_cached(str(ls.get('active_invoke') or ''))
        if not isinstance(tps, (int, float)) or tps <= 0:
            return ""
        return self._fmt_header_metric(tps, 'tps')

    def current_chat_header_tps(self, snapshot_seq: Optional[int] = None) -> str:
        """Return header TPS, preserving the last relevant message TPS."""
        live_tps = self.current_stream_tps()
        if live_tps:
            return live_tps

        if snapshot_seq is None:
            snapshot_seq = self._snapshot_last_event_seq(self.current_thread)
        cache = getattr(self, '_chat_header_tps_cache', None)
        cache_key = (self.current_thread, snapshot_seq)
        if isinstance(cache, dict) and cache.get('key') == cache_key:
            return str(cache.get('value') or "")

        try:
            msgs = snapshot_messages(self.db, self.current_thread)
        except Exception:
            msgs = []
        for m in reversed(msgs or []):
            if not isinstance(m, dict):
                continue
            if m.get('role') not in ('assistant', 'tool'):
                continue
            tps = m.get('tps')
            try:
                fv = float(tps)
            except Exception:
                continue
            if fv <= 0:
                continue
            value = self._fmt_header_metric(fv, 'tps')
            self._chat_header_tps_cache = {'key': cache_key, 'value': value}
            return value
        self._chat_header_tps_cache = {'key': cache_key, 'value': ""}
        return ""

    def render_group(self) -> Group:
        """Render the panel group for the live display."""
        # Single-column layout, top-to-bottom:
        #   (separator line)
        #   System
        #   Children
        #   Chat Messages (inline mode only)
        #   (optional) Approval
        #   Input
        from rich.rule import Rule
        children: List[Any] = [Rule(style="dim")]  # Separator between static and live
        if self._panel_visible.get('system', True):
            children.append(self.system_output.render())
        if self._panel_visible.get('children', True):
            children.append(self.children_output.render())
        if self._display_is_inline and self._panel_visible.get('chat', True):
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

    def _print_static_transcript_renderable(self, item: _StaticTranscriptRenderable) -> None:
        """Print one prebuilt static transcript renderable with its fallback."""
        self._print_static_transcript_renderable_via(
            item,
            lambda obj: self._live_print(obj),
        )

    def _print_static_transcript_renderable_via(
        self,
        item: _StaticTranscriptRenderable,
        printer: Any,
    ) -> Any:
        """Print one static transcript item using *printer*, falling back to text."""
        try:
            return printer(item.renderable)
        except Exception:
            fallback = item.fallback
            if fallback is None:
                fallback = getattr(item.renderable, 'plain', str(item.renderable))
            return printer(fallback)

    def console_print_compaction_marker(self, marker: Dict[str, Any]) -> None:
        """Print a visible compaction boundary divider to the console."""
        self._print_static_transcript_renderable(
            self._static_transcript_compaction_marker_renderable(marker)
        )

    def _static_transcript_compaction_marker_renderable(
        self,
        marker: Dict[str, Any],
    ) -> _StaticTranscriptRenderable:
        """Build the rich renderable for a static transcript compaction marker."""
        try:
            text = self._compaction_marker_text(marker)
        except Exception:
            start_msg_id = str(marker.get('start_msg_id') or '')
            start_short = start_msg_id[-8:] if start_msg_id else 'unknown'
            text = f"Compaction boundary: API context now starts at msg_{start_short}."
        try:
            renderable = Panel(
                Text(text, no_wrap=False, overflow='fold', style='bold red'),
                title='[bold red]Compaction Boundary[/bold red]',
                border_style='red',
                box=self._get_static_box(),
            )
        except Exception:
            renderable = text
        return _StaticTranscriptRenderable(renderable, text)

    def _panel_display_verbosity_level(self) -> str:
        """Return the current display verbosity for static console panels."""
        try:
            level = self._display_verbosity_level()
        except Exception:
            level = getattr(self, '_display_verbosity', 'max')
        level = str(level or 'max').strip().lower()
        return level if level in {'max', 'medium', 'min'} else 'max'

    def _panel_one_line_preview(self, text: Any, *, max_chars: int = 160) -> str:
        """Return a compact one-line preview for collapsed panel rows."""
        try:
            return self._one_line_display_preview(text, max_chars=max_chars)
        except Exception:
            preview = " ".join(str(text or '').split())
            if max_chars > 3 and len(preview) > max_chars:
                return preview[: max_chars - 3].rstrip() + "..."
            return preview

    def _new_static_hidden_details_state(self) -> Dict[str, Any]:
        return {
            'summary': MinHiddenActivitySummary(),
        }

    def _reset_static_min_run_tracking(self) -> None:
        """Forget any full-screen local row currently used for a min summary."""
        self._static_min_summary_row_count = 0

    def _reset_static_hidden_details(self) -> None:
        self._static_hidden_details = self._new_static_hidden_details_state()
        self._reset_static_min_run_tracking()

    def _ensure_static_hidden_details_state(
        self,
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if isinstance(state, dict):
            target = state
        else:
            target = getattr(self, '_static_hidden_details', None)
            if not isinstance(target, dict):
                self._reset_static_hidden_details()
                target = self._static_hidden_details
        summary = target.get('summary')
        if not isinstance(summary, MinHiddenActivitySummary):
            summary = MinHiddenActivitySummary()
            target['summary'] = summary
        return target

    def _clear_static_hidden_details_state(self, state: Dict[str, Any]) -> None:
        summary = state.get('summary')
        if isinstance(summary, MinHiddenActivitySummary):
            summary.clear()
        else:
            state['summary'] = MinHiddenActivitySummary()

    def _has_static_hidden_details_activity(
        self,
        state: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if state is None:
            state = getattr(self, '_static_hidden_details', None)
        if not isinstance(state, dict):
            return False
        summary = state.get('summary')
        return isinstance(summary, MinHiddenActivitySummary) and summary.has_activity()

    def _static_min_summary_token_count(self, tokens: Any, fallback_text: Any = "") -> int:
        try:
            iv = int(tokens)
        except Exception:
            iv = 0
        if iv > 0:
            return iv
        return count_min_hidden_text_tokens(fallback_text)

    def _record_static_hidden_detail_in_state(
        self,
        state: Dict[str, Any],
        kind: str,
        header: str,
        *,
        name: Any = None,
        tokens: Any = 0,
        tool_call_id: Optional[str] = None,
    ) -> None:
        target = self._ensure_static_hidden_details_state(state)
        summary = target.get('summary')
        if not isinstance(summary, MinHiddenActivitySummary):
            summary = MinHiddenActivitySummary()
            target['summary'] = summary
        if kind == 'reasoning':
            summary.add_reasoning_block(tokens=tokens)
        elif kind == 'tool_calls':
            summary.add_tool_execution(name=name, tokens=tokens, tool_call_id=tool_call_id)
        elif kind == 'tool_results':
            summary.add_tool_result(name=name, tokens=tokens)

    def _record_static_hidden_detail(self, kind: str, header: str) -> None:
        self._record_static_hidden_detail_in_state(
            self._ensure_static_hidden_details_state(),
            kind,
            header,
        )

    def _static_hidden_details_renderable(
        self,
        state: Optional[Dict[str, Any]] = None,
        *,
        consume: bool = True,
    ) -> Optional[_StaticTranscriptRenderable]:
        """Render pending min-verbosity hidden-detail state into a summary item."""
        if state is None:
            state = getattr(self, '_static_hidden_details', None)
        if not isinstance(state, dict):
            return None
        state = self._ensure_static_hidden_details_state(state)
        summary = state.get('summary')
        if not isinstance(summary, MinHiddenActivitySummary) or not summary.has_activity():
            return None
        body = format_min_hidden_activity_summary(summary)
        if not body:
            return None
        try:
            renderable = Panel(
                Text(body, no_wrap=False, overflow='fold', style='yellow'),
                border_style='yellow',
                box=self._get_static_box(),
            )
        except Exception:
            renderable = body
        if consume:
            self._clear_static_hidden_details_state(state)
        return _StaticTranscriptRenderable(renderable, body, 'min_summary')

    def _flush_static_hidden_details(self) -> None:
        item = self._static_hidden_details_renderable()
        if item is not None:
            self._print_static_transcript_renderable(item)
        self._reset_static_min_run_tracking()

    def _replace_full_screen_static_min_summary(
        self,
        item: _StaticTranscriptRenderable,
    ) -> bool:
        """Print/update the current local min summary row in full-screen mode."""
        renderer = getattr(self, '_renderer', None)
        if not (
            self._is_full_screen_scrollback_renderer(renderer)
            and hasattr(renderer, 'replace_recent_scrollback')
        ):
            return False
        previous_rows = max(0, int(getattr(self, '_static_min_summary_row_count', 0) or 0))

        def replace(renderable: Any) -> Any:
            return renderer.replace_recent_scrollback(previous_rows, renderable)

        try:
            new_rows = self._print_static_transcript_renderable_via(item, replace)
        except Exception:
            return False
        try:
            self._static_min_summary_row_count = max(0, int(new_rows or 0))
        except Exception:
            self._static_min_summary_row_count = 0
        return True

    def _update_full_screen_static_min_summary(self) -> bool:
        """Refresh the in-place full-screen summary for pending hidden activity."""
        if self._panel_display_verbosity_level() != 'min':
            return False
        renderer = getattr(self, '_renderer', None)
        if not (
            self._is_full_screen_scrollback_renderer(renderer)
            and hasattr(renderer, 'replace_recent_scrollback')
        ):
            return False
        state = self._ensure_static_hidden_details_state()
        if not self._has_static_hidden_details_activity(state):
            return False
        item = self._static_hidden_details_renderable(state, consume=False)
        if item is None:
            return False
        return self._replace_full_screen_static_min_summary(item)

    def _static_transcript_ts_text(self, val: Any) -> str:
        """Format a message timestamp for static transcript panel titles."""
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

    def _static_transcript_message_token_counts(self, msg_id: Any) -> Dict[str, int]:
        """Best-effort per-message token counts for static transcript titles."""
        pm_tokens: Dict[str, int] = {"content": 0, "reasoning": 0, "tool_calls": 0, "total": 0}
        try:
            if msg_id:
                # Full-screen min scrollback renders blocks lazily as the user
                # scrolls. Loading/parsing the full snapshot JSON for every
                # block is O(history) per wheel/input repaint on large threads;
                # cache the per-message token map once per snapshot watermark.
                snapshot_seq = self._snapshot_last_event_seq(self.current_thread)
                cache_key = (self.current_thread, snapshot_seq)
                cache = getattr(self, '_static_transcript_token_counts_cache', None)
                if not isinstance(cache, dict) or cache.get('key') != cache_key:
                    cache = {
                        'key': cache_key,
                        'per_message': snapshot_per_message_token_stats(self.db, self.current_thread),
                    }
                    self._static_transcript_token_counts_cache = cache
                pm = cache.get('per_message') or {}
                if isinstance(pm, dict) and str(msg_id) in pm:
                    info = pm[str(msg_id)] or {}
                    pm_tokens["content"] = int(info.get('content_tokens') or 0)
                    pm_tokens["reasoning"] = int(info.get('reasoning_tokens') or 0)
                    pm_tokens["tool_calls"] = int(info.get('tool_calls_tokens') or 0)
                    pm_tokens["total"] = int(info.get('total_tokens') or (
                        pm_tokens["content"] + pm_tokens["reasoning"] + pm_tokens["tool_calls"]
                    ))
        except Exception:
            pm_tokens = {"content": 0, "reasoning": 0, "tool_calls": 0, "total": 0}
        return pm_tokens

    def _static_transcript_panel_renderable(
        self,
        renderable: Any,
        title: str,
        border: str,
        *,
        fallback: Optional[str] = None,
    ) -> _StaticTranscriptRenderable:
        """Build a static transcript Panel renderable with a plain fallback."""
        if fallback is None:
            fallback = f"[{border}]{title}[/] {getattr(renderable, 'plain', str(renderable))}"
        try:
            panel_renderable = Panel(
                renderable,
                title=title,
                border_style=border,
                box=self._get_static_box(),
            )
        except Exception:
            panel_renderable = fallback
        return _StaticTranscriptRenderable(panel_renderable, fallback)

    def _static_transcript_message_renderables(
        self,
        m: Dict[str, Any],
        hidden_details: Optional[Dict[str, Any]] = None,
    ) -> List[_StaticTranscriptRenderable]:
        """Build rich renderables for one static transcript message without printing.

        ``hidden_details`` carries min-verbosity summary state across messages.
        Callers that render out of band (for example a virtual scrollback
        source) can pass their own state to avoid touching console-printer
        state.
        """
        items: List[_StaticTranscriptRenderable] = []
        if hidden_details is None:
            hidden_details = self._new_static_hidden_details_state()
        else:
            hidden_details = self._ensure_static_hidden_details_state(hidden_details)
        role = m.get('role')
        content = (m.get('content') or '').strip()
        model_key = (m.get('model_key') or '').strip()
        msg_id = m.get('msg_id') or ''
        ts_str = self._static_transcript_ts_text(m.get('ts'))
        verbosity = self._panel_display_verbosity_level()
        msg_tps = self._fmt_header_metric(m.get('tps'), 'tps')
        pm_tokens = self._static_transcript_message_token_counts(msg_id)

        def full_title_for(title: str) -> str:
            # Build a unified title with optional timestamp and msg_id.
            parts = [title]
            if ts_str:
                parts.append(f"[dim]{ts_str}[/dim]")
            if msg_id:
                parts.append(f"[dim]msg_id: {msg_id}[/dim]")
            return " | ".join(parts)

        def append_hidden_details() -> None:
            item = self._static_hidden_details_renderable(hidden_details)
            if item is not None:
                items.append(item)

        def panel(renderable: Any, title: str, border: str) -> str:
            full_title = full_title_for(title)
            items.append(self._static_transcript_panel_renderable(
                renderable,
                full_title,
                border,
            ))
            return full_title

        if role == 'system':
            is_recovery_notice = bool(m.get('recovery_notice'))
            title = '[bold magenta]Continue Status[/bold magenta]' if is_recovery_notice else '[bold blue]System[/bold blue]'
            style = 'magenta' if is_recovery_notice else 'blue'
            if isinstance(content, str) and content.lower().startswith('llm error:'):
                title = '[bold red]Error[/bold red]'
                if verbosity == 'min':
                    append_hidden_details()
                panel(Text(content, no_wrap=False, overflow='fold', style='red'), title, 'red')
                return items
            if verbosity == 'min':
                append_hidden_details()
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            panel(Text(content, no_wrap=False, overflow='fold', style=style), title, style)
            return items

        if role == 'user':
            if verbosity == 'min':
                append_hidden_details()
            title = '[bold green]User[/bold green]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            # Attach content token count if available
            if pm_tokens["content"]:
                tok_text = self._fmt_header_metric(pm_tokens['content'], 'tok')
                if tok_text:
                    title += f" [dim]({tok_text})[/dim]"
            panel(Text(content, no_wrap=False, overflow='fold', style='green'), title, 'green')
            return items

        if role == 'assistant':
            is_assistant_note = bool(m.get('answer_user_preserve_turn'))
            title = '[bold bright_magenta]Assistant Note[/bold bright_magenta]' if is_assistant_note else '[bold cyan]Assistant[/bold cyan]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            if pm_tokens["content"]:
                tok_text = self._fmt_header_metric(pm_tokens['content'], 'tok')
                if tok_text:
                    title += f" [dim]({tok_text})[/dim]"
            if msg_tps:
                title += f" [dim]({msg_tps})[/dim]"
            # Prefer to show reasoning first if present
            reas = m.get('reasoning') or m.get('reasoning_content')
            if not is_assistant_note and isinstance(reas, str) and reas.strip():
                reason_title = '[bold magenta]Reasoning[/bold magenta]'
                if model_key:
                    reason_title += f" [dim](model: {model_key})[/dim]"
                if pm_tokens["reasoning"]:
                    tok_text = self._fmt_header_metric(pm_tokens['reasoning'], 'tok')
                    if tok_text:
                        reason_title += f" [dim]({tok_text})[/dim]"
                if msg_tps:
                    reason_title += f" [dim]({msg_tps})[/dim]"
                if verbosity == 'max':
                    panel(Text(reas, no_wrap=False, overflow='fold', style='magenta'), reason_title, 'magenta')
                elif verbosity == 'medium':
                    panel(Text('', no_wrap=False, overflow='fold', style='magenta'), reason_title, 'magenta')
                else:
                    self._record_static_hidden_detail_in_state(
                        hidden_details,
                        'reasoning',
                        full_title_for(reason_title),
                        tokens=self._static_min_summary_token_count(pm_tokens["reasoning"], reas),
                    )
            if content:
                if verbosity == 'min':
                    append_hidden_details()
                if is_assistant_note:
                    panel(Markdown(content), title, 'bright_magenta')
                elif looks_markdown(content):
                    panel(Markdown(content), title, 'cyan')
                else:
                    panel(Text(content, no_wrap=False, overflow='fold', style='white'), title, 'cyan')
            # Tool-calls summary if present
            tcs = m.get('tool_calls')
            if isinstance(tcs, list) and tcs:
                lines = []
                tool_call_infos = []
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
                    tc_id = str((tc or {}).get('id') or (tc or {}).get('tool_call_id') or '')
                    tool_call_infos.append((name, args_str, tc_id))
                    if verbosity == 'max':
                        lines.append(f"{name}({args_str})")
                    else:
                        tc_id_text = f" [tool_call_id: {tc_id}]" if tc_id else ""
                        preview = self._panel_one_line_preview(args_str)
                        suffix = f" {preview}" if preview else ""
                        lines.append(f"{name}{tc_id_text}{suffix}")
                # Build consistent title bar like other boxes
                tc_title_parts = ['[bold yellow]Tool Calls[/bold yellow]']
                if model_key:
                    tc_title_parts.append(f"[dim](model: {model_key})[/dim]")
                if pm_tokens["tool_calls"]:
                    tok_text = self._fmt_header_metric(pm_tokens['tool_calls'], 'tok')
                    if tok_text:
                        tc_title_parts.append(f"[dim]({tok_text})[/dim]")
                if msg_tps:
                    tc_title_parts.append(f"[dim]({msg_tps})[/dim]")
                if ts_str:
                    tc_title_parts.append(f"[dim]{ts_str}[/dim]")
                if msg_id:
                    tc_title_parts.append(f"[dim]msg_id: {msg_id}[/dim]")
                tc_title = " | ".join(tc_title_parts)
                if verbosity == 'min':
                    fallback_text = serialize_min_tool_call_tokens(tcs)
                    token_count = self._static_min_summary_token_count(pm_tokens["tool_calls"], fallback_text)
                    for idx, (name, _args_str, tc_id) in enumerate(tool_call_infos):
                        self._record_static_hidden_detail_in_state(
                            hidden_details,
                            'tool_calls',
                            f"{tc_title} | {lines[idx] if idx < len(lines) else name}",
                            name=name,
                            tokens=token_count if idx == 0 else 0,
                            tool_call_id=tc_id,
                        )
                else:
                    items.append(self._static_transcript_panel_renderable(
                        Text("\n".join(lines), no_wrap=False, overflow='fold', style='bold yellow'),
                        tc_title,
                        'yellow',
                    ))
            # Streamed-only metadata if present in snapshot (optional)
            tstream = m.get('tool_stream') or {}
            if isinstance(tstream, dict) and tstream:
                for nm, txt in tstream.items():
                    if txt:
                        out_title = f'[bold yellow]Tool Output: {nm}[/bold yellow]'
                        if verbosity == 'max':
                            items.append(self._static_transcript_panel_renderable(
                                Text(txt, no_wrap=False, overflow='fold', style='yellow'),
                                out_title,
                                'yellow',
                            ))
                        elif verbosity == 'medium':
                            title_parts = [out_title]
                            if model_key:
                                title_parts.append(f"[dim](model: {model_key})[/dim]")
                            if msg_tps:
                                title_parts.append(f"[dim]({msg_tps})[/dim]")
                            if ts_str:
                                title_parts.append(f"[dim]{ts_str}[/dim]")
                            if msg_id:
                                title_parts.append(f"[dim]msg_id: {msg_id}[/dim]")
                            items.append(self._static_transcript_panel_renderable(
                                Text('', no_wrap=False, overflow='fold', style='yellow'),
                                " | ".join(title_parts),
                                'yellow',
                            ))
                        else:
                            title_parts = [out_title]
                            if model_key:
                                title_parts.append(f"[dim](model: {model_key})[/dim]")
                            if msg_tps:
                                title_parts.append(f"[dim]({msg_tps})[/dim]")
                            if ts_str:
                                title_parts.append(f"[dim]{ts_str}[/dim]")
                            if msg_id:
                                title_parts.append(f"[dim]msg_id: {msg_id}[/dim]")
                            self._record_static_hidden_detail_in_state(
                                hidden_details,
                                'tool_results',
                                " | ".join(title_parts),
                                name=nm,
                                tokens=count_min_hidden_text_tokens(txt),
                            )
            tc_stream = m.get('tool_calls_stream') or {}
            if isinstance(tc_stream, dict) and tc_stream:
                for nm, txt in tc_stream.items():
                    if txt:
                        call_title = f'[bold yellow]Tool Call Args (streamed): {nm}[/bold yellow]'
                        if verbosity == 'max':
                            items.append(self._static_transcript_panel_renderable(
                                Text(txt, no_wrap=False, overflow='fold', style='yellow'),
                                call_title,
                                'yellow',
                            ))
                        elif verbosity == 'medium':
                            preview = self._panel_one_line_preview(txt)
                            title_parts = [call_title]
                            if model_key:
                                title_parts.append(f"[dim](model: {model_key})[/dim]")
                            if msg_tps:
                                title_parts.append(f"[dim]({msg_tps})[/dim]")
                            if ts_str:
                                title_parts.append(f"[dim]{ts_str}[/dim]")
                            if msg_id:
                                title_parts.append(f"[dim]msg_id: {msg_id}[/dim]")
                            items.append(self._static_transcript_panel_renderable(
                                Text(preview, no_wrap=False, overflow='fold', style='yellow'),
                                " | ".join(title_parts),
                                'yellow',
                            ))
                        else:
                            title_parts = [call_title]
                            if model_key:
                                title_parts.append(f"[dim](model: {model_key})[/dim]")
                            if msg_tps:
                                title_parts.append(f"[dim]({msg_tps})[/dim]")
                            if ts_str:
                                title_parts.append(f"[dim]{ts_str}[/dim]")
                            if msg_id:
                                title_parts.append(f"[dim]msg_id: {msg_id}[/dim]")
                            self._record_static_hidden_detail_in_state(
                                hidden_details,
                                'tool_calls',
                                " | ".join(title_parts),
                                name=nm,
                                tokens=count_min_hidden_text_tokens(txt),
                                tool_call_id=str(nm or ''),
                            )
            return items

        if role == 'tool':
            name = m.get('name') or 'Tool'
            if verbosity == 'max':
                title = f'[bold yellow]{name}[/bold yellow]'
            else:
                label_name = f"User Tool: {name}" if m.get('user_tool_call') else str(name)
                title = f'[bold yellow]{label_name}[/bold yellow]'
            if model_key:
                title += f" [dim](model: {model_key})[/dim]"
            # For tool messages, content tokens are the primary signal.
            if pm_tokens["content"]:
                tok_text = self._fmt_header_metric(pm_tokens['content'], 'tok')
                if tok_text:
                    title += f" [dim]({tok_text})[/dim]"
            if msg_tps:
                title += f" [dim]({msg_tps})[/dim]"
            tool_call_id = str(m.get('tool_call_id') or '')
            if tool_call_id and verbosity != 'max':
                title += f" [dim]tool_call_id: {tool_call_id}[/dim]"
            if verbosity == 'max':
                panel(Text(content, no_wrap=False, overflow='fold', style='yellow'), title, 'yellow')
            elif verbosity == 'medium':
                panel(Text('', no_wrap=False, overflow='fold', style='yellow'), title, 'yellow')
            else:
                self._record_static_hidden_detail_in_state(
                    hidden_details,
                    'tool_results',
                    full_title_for(title),
                    name=name,
                    tokens=self._static_min_summary_token_count(pm_tokens["content"], content),
                )
            return items

        # Fallback generic
        title = (role or 'Message')
        if model_key:
            title += f" [dim](model: {model_key})[/dim]"
        panel(Text(content, no_wrap=False, overflow='fold', style='blue'), title, 'blue')
        return items

    def console_print_message(self, m: Dict[str, Any]) -> None:
        """Print a single message to the console with rich formatting."""
        hidden_details = self._ensure_static_hidden_details_state()
        before_hidden = self._has_static_hidden_details_activity(hidden_details)
        items = self._static_transcript_message_renderables(m, hidden_details)

        if not items:
            if before_hidden or self._has_static_hidden_details_activity(hidden_details):
                self._update_full_screen_static_min_summary()
            return

        for item in items:
            if item.kind == 'min_summary':
                if not self._replace_full_screen_static_min_summary(item):
                    self._print_static_transcript_renderable(item)
            else:
                self._print_static_transcript_renderable(item)
            self._reset_static_min_run_tracking()

    def console_print_block(self, title: str, text: str, border_style: str = 'blue', markup: bool = True) -> None:
        """Print a titled block to the console."""
        try:
            if markup and ('[' in text and ']' in text):
                # Text contains Rich markup - parse it via Text.from_markup
                styled_text = Text.from_markup(text)
                self._live_print(Panel(styled_text, title=title, border_style=border_style, box=self._get_static_box()))
            else:
                # Plain text - apply border_style as text color
                styled_text = Text(text, style=border_style)
                self._live_print(Panel(styled_text, title=title, border_style=border_style, box=self._get_static_box()))
        except Exception as e:
            # Fallback plain - show error for debugging
            import traceback
            self._live_print(f"{title}\n{text}\n[Error: {e}]\n{traceback.format_exc()}")

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
                self._live_print(Panel(heading, border_style='blue', box=self._get_static_box()))
            except Exception:
                self._live_print(heading)
        msgs = snapshot_messages(self.db, tid)
        markers_by_start_seq = self._compaction_markers_by_start_seq(tid)
        verbosity = self._panel_display_verbosity_level()
        if verbosity == 'min':
            self._reset_static_hidden_details()
        if not msgs and not markers_by_start_seq:
            self._live_print(Panel('[dim]No messages yet[/dim]', border_style='blue', box=self._get_static_box()))
        else:
            for m in msgs:
                if isinstance(m, dict):
                    try:
                        event_seq_int = int(m.get('event_seq'))
                    except Exception:
                        event_seq_int = -1
                    for marker in markers_by_start_seq.get(event_seq_int, []):
                        if verbosity == 'min':
                            self._flush_static_hidden_details()
                        self.console_print_compaction_marker(marker)
                    self.console_print_message(m)
            if verbosity == 'min':
                self._flush_static_hidden_details()
        # Update last-printed seq to the latest rendered transcript event so we don't re-print.
        self._mark_static_transcript_printed(tid)

    def print_banner(self) -> None:
        """Print the static console banner (above the live panels)."""
        try:
            self.console.print("[bold blue]Egg Chat (eggdisplay UI)[/bold blue]")
            self.console.print(
                "Press Enter or Ctrl+D to send. Shift+Enter or Alt+Enter inserts a newline. "
                "Ctrl+E clears input. Ctrl+P paste, Ctrl+C to quit. Type /help for commands.\n"
            )
        except Exception:
            pass

    def redraw_static_view(self, *, reason: str = '') -> None:
        """Clear terminal and reprint static transcript for current thread."""
        renderer = getattr(self, '_renderer', None)
        if self._is_full_screen_scrollback_renderer(renderer):
            self._install_transcript_scrollback_source(
                renderer,
                reset_session_scrollback=True,
                repaint=True,
            )
            return

        # Inline/non-renderer redraw keeps using native terminal scrollback and
        # full static transcript printing. Only the full-screen renderer swaps
        # in a lazy source above.
        if renderer is not None:
            if hasattr(renderer, 'invalidate'):
                renderer.invalidate()
        else:
            try:
                self.console.clear()
            except Exception:
                pass
        self.print_banner()
        heading = f"Redraw: {reason}\nSwitched to thread: {self.current_thread}" if reason else f"Switched to thread: {self.current_thread}"
        self.print_static_view_current(heading=heading)
