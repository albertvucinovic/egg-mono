"""Panel management mixin for the egg application."""
from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from rich.console import Console, Group
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich import box as rich_box

from eggthreads import create_snapshot
from eggthreads.content_parts import content_to_plain_text
from eggthreads.output_optimizer.observability import format_output_optimizer_summary

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
GET_USER_INPUT_RELEVANT_EVENT_TYPES = frozenset({
    'msg.create',
    'msg.edit',
    'msg.delete',
    'stream.open',
    'stream.close',
    'control.interrupt',
    'tool_call.approval',
    'tool_call.execution_started',
    'tool_call.finished',
    'tool_call.output_approval',
})


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
        # Capture before any snapshot/load work. A semantic event observed while
        # this source is being built must make later reuse fail closed.
        try:
            self._local_transcript_generation = int(
                panels._static_transcript_generation(self._thread_id)
            )
        except Exception:
            self._local_transcript_generation = -1
        if refresh_snapshot:
            self._refresh_snapshot_if_safe()
        self._snapshot_seq = -1
        self._per_message_token_stats: Dict[str, Dict[str, Any]] = {}
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
            row = self._db.get_thread(self._thread_id)
            self._snapshot_seq = int(row.snapshot_last_event_seq) if row is not None else -1
            raw_snapshot = getattr(row, 'snapshot_json', None) if row is not None else None
            snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) and raw_snapshot else {}
            msgs = snapshot.get('messages') if isinstance(snapshot, dict) else []
            if not isinstance(msgs, list):
                msgs = []
            token_stats = snapshot.get('token_stats') if isinstance(snapshot, dict) else None
            per_message = token_stats.get('per_message') if isinstance(token_stats, dict) else None
            if isinstance(per_message, dict):
                self._per_message_token_stats = {
                    str(msg_id): info
                    for msg_id, info in per_message.items()
                    if isinstance(msg_id, str) and isinstance(info, dict)
                }
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
            verbosity = str(getattr(self._panels, '_display_verbosity', 'min') or 'min')
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
        content = content_to_plain_text(m.get('content')).strip()
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
                    per_message_token_stats=self._per_message_token_stats,
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
        theme = getattr(self._panels, '_rich_theme', None)

        fallback = item.fallback
        if fallback is None:
            fallback = getattr(item.renderable, 'plain', str(item.renderable))
        for renderable in (item.renderable, fallback):
            try:
                buf = io.StringIO()
                kwargs = {
                    'file': buf,
                    'width': width,
                    'force_terminal': True,
                    'color_system': color_system or 'truecolor',
                }
                if theme is not None:
                    kwargs['theme'] = theme
                console = Console(**kwargs)
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

    def _static_transcript_generation(self, thread_id: Optional[str] = None) -> int:
        """Return the app-local semantic transcript generation for a thread."""
        tid = thread_id or self.current_thread
        generations = getattr(self, '_static_transcript_generation_by_thread', None)
        if not isinstance(generations, dict):
            generations = {}
            self._static_transcript_generation_by_thread = generations
        try:
            return int(generations.get(tid, 0) or 0)
        except Exception:
            return 0

    def _mark_static_transcript_changed(self, thread_id: Optional[str] = None) -> int:
        """Invalidate source reuse after a watcher-observed semantic change."""
        tid = thread_id or self.current_thread
        generations = getattr(self, '_static_transcript_generation_by_thread', None)
        if not isinstance(generations, dict):
            generations = {}
            self._static_transcript_generation_by_thread = generations
        generation = self._static_transcript_generation(tid) + 1
        generations[tid] = generation
        return generation

    def _new_transcript_scrollback_source(self) -> TranscriptScrollbackSource:
        """Create a fresh lazy transcript source for the current thread."""
        return TranscriptScrollbackSource(self)

    def _coherent_transcript_scrollback_source(
        self,
        renderer: Any,
    ) -> Optional[TranscriptScrollbackSource]:
        """Return the installed source only when all reuse invariants match."""
        state = getattr(self, '_transcript_scrollback_source_state', None)
        if not (isinstance(state, tuple) and len(state) == 2 and state[0] is renderer):
            return None
        source = state[1]
        if not isinstance(source, TranscriptScrollbackSource):
            return None
        if source._thread_id != self.current_thread:
            return None
        if source._local_transcript_generation != self._static_transcript_generation():
            return None
        try:
            row = self.db.get_thread_metadata(self.current_thread)
            current_snapshot_seq = int(row.snapshot_last_event_seq) if row is not None else -1
        except Exception:
            return None
        if current_snapshot_seq != int(source._snapshot_seq):
            return None
        return source

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
        source: Optional[TranscriptScrollbackSource] = None,
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
            if source is None:
                source = self._new_transcript_scrollback_source()
            reset_source = getattr(renderer, 'reset_scrollback_source', None)
            if reset_session_scrollback and callable(reset_source):
                # The canonical renderer reset does not paint. The optional
                # update below supplies the one final frame.
                reset_source(source)
            else:
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
            self._transcript_scrollback_source_state = (renderer, source)
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
        """Return an indexed monotonic key for Children topology and status.

        Relevant event history can contain millions of rows.  For each subtree
        thread, ask the ``events_thread_type`` index only for the latest matching
        sequence rather than counting/scanning every historical event.
        """
        placeholders = ', '.join('?' for _ in CHILDREN_PANEL_RELEVANT_EVENT_TYPES)
        cur = self.db.conn.execute(
            f"""
            WITH RECURSIVE subtree(thread_id) AS (
                SELECT ?
                UNION
                SELECT c.child_id
                FROM children c
                JOIN subtree s ON c.parent_id=s.thread_id
            ), topology AS (
                SELECT COUNT(*) AS child_count, COALESCE(MAX(c.rowid), 0) AS child_max
                FROM children c
                JOIN subtree s ON s.thread_id=c.parent_id
            ), relevant_event_heads AS (
                SELECT COALESCE(GROUP_CONCAT(event_head, '|'), '') AS event_key
                FROM (
                    SELECT s.thread_id || ':' || COALESCE((
                        SELECT MAX(e.event_seq)
                        FROM events e INDEXED BY events_thread_type
                        WHERE e.thread_id=s.thread_id
                          AND e.type IN ({placeholders})
                    ), -1) AS event_head
                    FROM subtree s
                    ORDER BY s.thread_id
                )
            ), active_streams AS (
                SELECT
                    COUNT(*) AS open_count,
                    COALESCE(GROUP_CONCAT(open_key, '|'), '') AS open_key
                FROM (
                    SELECT o.thread_id || ':' || o.invoke_id || ':' || COALESCE(o.purpose, '') AS open_key
                    FROM open_streams o
                    JOIN subtree s ON s.thread_id=o.thread_id
                    WHERE o.lease_until > datetime('now')
                    ORDER BY o.thread_id, o.invoke_id
                )
            )
            SELECT
                topology.child_count,
                topology.child_max,
                relevant_event_heads.event_key,
                active_streams.open_count,
                active_streams.open_key,
                COALESCE(t.name, ''),
                COALESCE(t.short_recap, '')
            FROM topology, relevant_event_heads, active_streams
            LEFT JOIN threads t ON t.thread_id=?
            """,
            (
                self.current_thread,
                *CHILDREN_PANEL_RELEVANT_EVENT_TYPES,
                self.current_thread,
            ),
        )
        row = cur.fetchone()
        if not row:
            return (self.current_thread, 0, 0, '', 0, '', '', '')
        return (
            self.current_thread,
            int(row[0] or 0),
            int(row[1] or 0),
            str(row[2] or ''),
            int(row[3] or 0),
            str(row[4] or ''),
            str(row[5] or ''),
            str(row[6] or ''),
        )

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
        self._apply_get_user_message_input_mode()

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
        api_usage: Dict[str, Any] = {}
        try:
            ctx_tokens, api_usage = self.current_token_stats(snapshot_seq=snapshot_seq)
            if not isinstance(api_usage, dict):
                api_usage = {}

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
                try:
                    cost_text = self.header_cost_metric(api_usage)
                except Exception:
                    cost_text = ""
                if cost_text:
                    title_parts.append(cost_text)
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
            try:
                cost_text = self.header_cost_metric(api_usage)
            except Exception:
                cost_text = ""
            if cost_text:
                metric_parts.append(cost_text)

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
        title = "  ".join(title_parts)
        self.system_output.title = title

        # Keep the normal System panel to one title row. Streaming status can
        # be longer and changes frequently, so render it in a dedicated second
        # row instead of extending the title border.
        self.system_output.set_content(stream_part or "")
        if not stream_part:
            try:
                if float(getattr(self.system_output, 'current_height', 2)) != 2:
                    self.system_output.current_height = 2
                    self.system_output.mark_dirty()
            except Exception:
                pass

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
                        subtree_text = self.format_children_panel(self.current_thread)
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

    def _refresh_get_user_message_input_mode(self) -> None:
        """Refresh cached get-user waiting state after relevant durable events."""
        try:
            from eggthreads import get_active_get_user_message_waiting_note

            waiting_note = get_active_get_user_message_waiting_note(
                self.db, self.current_thread
            )
        except Exception:
            waiting_note = None
        self._get_user_input_mode_thread = self.current_thread
        self._get_user_input_waiting = waiting_note is not None
        self._apply_get_user_message_input_mode()

    def _update_get_user_message_input_mode(self) -> None:
        """Compatibility wrapper for explicit callers that require a refresh."""
        self._refresh_get_user_message_input_mode()

    def _apply_get_user_message_input_mode(self) -> None:
        """Apply cached input styling without projecting tool state."""
        try:
            if not hasattr(self, '_normal_input_panel_title'):
                self._normal_input_panel_title = self.input_panel.title
                self._normal_input_border_style = self.input_panel.style.border_style
                self._normal_input_title_style = self.input_panel.style.title_style
            waiting = (
                getattr(self, '_get_user_input_mode_thread', None) == self.current_thread
                and bool(getattr(self, '_get_user_input_waiting', False))
            )
            if waiting:
                title = "Message Input (get answer tool)"
                border = "magenta"
                title_style = ""
            else:
                title = self._normal_input_panel_title
                border = self._normal_input_border_style
                title_style = self._normal_input_title_style
                try:
                    count = int(self._staged_attachment_count_for_current_thread())
                except Exception:
                    count = 0
                if count > 0:
                    suffix = f" — {count} attachment{'s' if count != 1 else ''} staged"
                    if suffix not in str(title):
                        title = f"{title}{suffix}"
            changed = (
                self.input_panel.title != title
                or self.input_panel.style.border_style != border
                or self.input_panel.style.title_style != title_style
            )
            if changed:
                self.input_panel.title = title
                self.input_panel.style.border_style = border
                self.input_panel.style.title_style = title_style
                self.input_panel.mark_dirty()
        except Exception:
            pass

    def _current_stream_header_part(self, *, include_tps: bool = True) -> str:
        """Return a compact live-streaming status for the System panel.

        Empty when nothing is streaming. The caller renders this in the System
        panel's second row, not in the title, so long timeout/TPS text does not
        crowd out model/sandbox/approval status. For LLM streams, appends live
        TPS when available. For tool streams, appends the active tool name
        (best-effort) so users see what's running.
        """
        ls = getattr(self, '_live_state', {}) or {}
        invoke = ls.get('active_invoke')
        if not invoke:
            return ""
        kind = ls.get('stream_kind') or 'stream'
        if kind == 'llm':
            duration = self._current_provider_stream_duration()
            tps_str = ""
            if include_tps:
                tps = self._live_llm_tps_cached(str(invoke))
                if isinstance(tps, (int, float)) and tps > 0:
                    tps_str = self._fmt_header_metric(tps, 'tps')
            parts = ["llm"]
            if tps_str:
                parts.append(tps_str)
            if duration:
                parts.append(duration)
            inner = "; ".join(parts)
        elif kind == 'tool':
            tool_name = ""
            countdown = self._current_tool_timeout_countdown()
            summary = ls.get('tool_summary') if isinstance(ls.get('tool_summary'), dict) else {}
            if summary and summary.get('active') and summary.get('text'):
                inner = str(summary.get('text') or '')
            else:
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
                else:
                    tools = ls.get('tools') if isinstance(ls.get('tools'), dict) else {}
                    if tools:
                        try:
                            tool_name = next(iter(tools.keys())) or ""
                        except Exception:
                            tool_name = ""
                    inner = f"tool {tool_name}" if tool_name else "tool"
            if countdown and "timeout in" not in inner:
                inner = f"{inner}; {countdown}"
        elif kind == 'user command':
            command_name = str(ls.get('command_name') or '').strip()
            elapsed = self._current_user_command_duration()
            inner = f"user command /{command_name}" if command_name else "user command"
            if elapsed:
                inner = f"{inner}; {elapsed}"
        else:
            inner = str(kind)
        # Escape inner brackets so Rich doesn't try to parse them as markup.
        return f"[yellow]Streaming\\[{inner}][/yellow]"

    def _current_tool_timeout_countdown(self) -> str:
        """Return a local, non-persisted timeout countdown for an active tool stream."""

        ls = getattr(self, '_live_state', {}) or {}
        if not ls.get('active_invoke') or ls.get('stream_kind') != 'tool':
            return ""
        try:
            timeout = float(ls.get('timeout_sec'))
            started = float(ls.get('timeout_started_at') or ls.get('started_at'))
        except Exception:
            return ""
        if timeout <= 0 or started <= 0:
            return ""
        remaining = max(0.0, timeout - (time.time() - started))
        return f"timeout in {remaining:.0f}s (limit {timeout:.0f}s)"

    def _current_provider_stream_duration(self) -> str:
        """Return elapsed provider streaming time for the active LLM request."""

        ls = getattr(self, '_live_state', {}) or {}
        if not ls.get('active_invoke') or ls.get('stream_kind') != 'llm':
            return ""
        try:
            started = float(ls.get('provider_started_at'))
        except Exception:
            return ""
        if started <= 0:
            return ""
        elapsed = max(0.0, time.time() - started)
        try:
            limit = float(ls.get('provider_timeout_sec'))
        except Exception:
            limit = 0.0
        if limit > 0:
            try:
                last_activity = float(ls.get('provider_last_activity_at') or started)
            except Exception:
                last_activity = started
            inactive_for = max(0.0, time.time() - last_activity)
            remaining = max(0.0, limit - inactive_for)
            return f"streaming {elapsed:.0f}s; inactivity timeout in {remaining:.0f}s (limit {limit:.0f}s)"
        return f"streaming {elapsed:.0f}s"

    def _current_user_command_duration(self) -> str:
        """Return elapsed time for a locally running user command."""

        ls = getattr(self, '_live_state', {}) or {}
        if not ls.get('active_invoke') or ls.get('stream_kind') != 'user command':
            return ""
        try:
            started = float(ls.get('started_at'))
        except Exception:
            return ""
        if started <= 0:
            return ""
        elapsed = max(0.0, time.time() - started)
        return f"running {elapsed:.0f}s"

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
        self._mark_static_transcript_changed()
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
            level = getattr(self, '_display_verbosity', 'min')
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

    def _output_optimizer_summary(self, message: Dict[str, Any], *, include_artifact_id: bool = False) -> str:
        """Return compact optimizer metadata for display, if present."""

        metadata = message.get('output_optimizer') if isinstance(message, dict) else None
        if not isinstance(metadata, dict):
            return ''
        try:
            return format_output_optimizer_summary(metadata, include_artifact_id=include_artifact_id)
        except Exception:
            return ''

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
        # SQLite event timestamps are UTC. Show them in the user's current
        # local timezone for panel headers.
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
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
            panel_title = Text.from_markup(str(title), style=border)
        except Exception:
            panel_title = title
        try:
            panel_renderable = Panel(
                renderable,
                title=panel_title,
                border_style=border,
                box=self._get_static_box(),
            )
        except Exception:
            panel_renderable = fallback
        return _StaticTranscriptRenderable(panel_renderable, fallback)

    def _tool_call_style(self, variant: str = "body") -> str:
        """Return the terminal style for tool-call arguments.

        Custom themes provide a distinct tool-call color. Without an active
        Egg theme, keep the historical Rich yellow styling.
        """
        themed = {
            "body": "egg.tool_call",
            "dim": "egg.tool_call_dim",
            "title": "egg.tool_call_title",
        }
        fallback = {
            "body": "yellow",
            "dim": "dim yellow",
            "title": "bold yellow",
        }
        if getattr(self, '_rich_theme', None) is not None:
            return themed.get(variant, themed["body"])
        return fallback.get(variant, fallback["body"])

    def _assistant_body_style(self, *, markdown: bool = False, note: bool = False) -> Optional[str]:
        """Return assistant body style without changing the default theme."""
        if getattr(self, '_rich_theme', None) is not None:
            return "egg.reasoning" if note else "egg.assistant"
        if markdown or note:
            return None
        return "white"

    def _assistant_border_style(self, *, note: bool = False) -> str:
        if getattr(self, '_rich_theme', None) is not None:
            return "egg.reasoning" if note else "egg.assistant"
        return 'bright_magenta' if note else 'cyan'

    def _semantic_title_label(self, label: str, *, semantic: str, default_style: str) -> str:
        """Return a styled panel-title label for the active terminal theme."""
        if getattr(self, '_rich_theme', None) is not None:
            themed_styles = {
                "user": "egg.user",
                "assistant": "egg.assistant",
                "system": "egg.system",
                "tool": "egg.tool",
                "reasoning": "egg.reasoning",
            }
            style = themed_styles.get(semantic, default_style)
        else:
            style = default_style
        return f"[{style}]{rich_escape(str(label))}[/{style}]"

    def _static_transcript_message_renderables(
        self,
        m: Dict[str, Any],
        hidden_details: Optional[Dict[str, Any]] = None,
        *,
        per_message_token_stats: Optional[Dict[str, Dict[str, Any]]] = None,
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
        content = content_to_plain_text(m.get('content')).strip()
        model_key = (m.get('model_key') or '').strip()
        msg_id = m.get('msg_id') or ''
        ts_str = self._static_transcript_ts_text(m.get('ts'))
        verbosity = self._panel_display_verbosity_level()
        msg_tps = self._fmt_header_metric(m.get('tps'), 'tps')
        if per_message_token_stats is None:
            pm_tokens = self._static_transcript_message_token_counts(msg_id)
        else:
            info = per_message_token_stats.get(str(msg_id), {}) if msg_id else {}
            pm_tokens = {
                "content": int(info.get('content_tokens') or 0),
                "reasoning": int(info.get('reasoning_tokens') or 0),
                "tool_calls": int(info.get('tool_calls_tokens') or 0),
                "total": int(info.get('total_tokens') or 0),
            }

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
            title = self._semantic_title_label(
                'Continue Status',
                semantic='reasoning',
                default_style='bold magenta',
            ) if is_recovery_notice else self._semantic_title_label(
                'System',
                semantic='system',
                default_style='bold blue',
            )
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
            display_content = (
                self._panel_one_line_preview(content)
                if verbosity == 'min' and is_recovery_notice
                else content
            )
            panel(Text(display_content, no_wrap=False, overflow='fold', style=style), title, style)
            return items

        if role == 'user':
            if verbosity == 'min':
                append_hidden_details()
            title = self._semantic_title_label('User', semantic='user', default_style='bold green')
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
            title = self._semantic_title_label(
                'Assistant Note',
                semantic='reasoning',
                default_style='bold bright_magenta',
            ) if is_assistant_note else self._semantic_title_label(
                'Assistant',
                semantic='assistant',
                default_style='bold cyan',
            )
            assistant_border = self._assistant_border_style(note=is_assistant_note)
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
                reason_title = self._semantic_title_label('Reasoning', semantic='reasoning', default_style='bold magenta')
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
                assistant_markdown_style = self._assistant_body_style(markdown=True, note=is_assistant_note)
                assistant_text_style = self._assistant_body_style(markdown=False, note=is_assistant_note)
                if is_assistant_note:
                    panel(Markdown(content, style=assistant_markdown_style or 'none'), title, assistant_border)
                elif looks_markdown(content):
                    panel(Markdown(content, style=assistant_markdown_style or 'none'), title, assistant_border)
                else:
                    panel(Text(content, no_wrap=False, overflow='fold', style=assistant_text_style or ''), title, assistant_border)
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
                tool_call_title_style = self._tool_call_style("title")
                tool_call_body_style = self._tool_call_style("body")
                tool_call_border_style = self._tool_call_style("body")
                tc_title_parts = [f'[{tool_call_title_style}]Tool Calls[/{tool_call_title_style}]']
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
                        Text("\n".join(lines), no_wrap=False, overflow='fold', style=tool_call_body_style),
                        tc_title,
                        tool_call_border_style,
                    ))
            # Streamed-only metadata if present in snapshot (optional)
            tstream = m.get('tool_stream') or {}
            if isinstance(tstream, dict) and tstream:
                for nm, txt in tstream.items():
                    if txt:
                        out_title = self._semantic_title_label(
                            f'Tool Output: {nm}',
                            semantic='tool',
                            default_style='bold yellow',
                        )
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
                        tool_call_title_style = self._tool_call_style("title")
                        tool_call_body_style = self._tool_call_style("body")
                        tool_call_border_style = self._tool_call_style("body")
                        call_title = f'[{tool_call_title_style}]Tool Call Args (streamed): {nm}[/{tool_call_title_style}]'
                        if verbosity == 'max':
                            items.append(self._static_transcript_panel_renderable(
                                Text(txt, no_wrap=False, overflow='fold', style=tool_call_body_style),
                                call_title,
                                tool_call_border_style,
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
                                Text(preview, no_wrap=False, overflow='fold', style=tool_call_body_style),
                                " | ".join(title_parts),
                                tool_call_border_style,
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
                title = self._semantic_title_label(str(name), semantic='tool', default_style='bold yellow')
            else:
                label_name = f"User Tool: {name}" if m.get('user_tool_call') else str(name)
                title = self._semantic_title_label(label_name, semantic='tool', default_style='bold yellow')
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
            optimizer_summary = self._output_optimizer_summary(m, include_artifact_id=True)
            if optimizer_summary:
                title += f" [dim]{rich_escape(optimizer_summary)}[/dim]"
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

    def console_print_message(self, m: Dict[str, Any], *, defer_min_summary: bool = False) -> None:
        """Print a message, optionally deferring a growing min-run repaint.

        Streaming content was already visible before its completed message is
        published.  A watcher batch can therefore accumulate consecutive
        hidden messages and repaint the final aggregate once without reducing
        live observability.
        """
        self._mark_static_transcript_changed()
        hidden_details = self._ensure_static_hidden_details_state()
        before_hidden = self._has_static_hidden_details_activity(hidden_details)
        items = self._static_transcript_message_renderables(m, hidden_details)

        if not items:
            if (
                not defer_min_summary
                and (before_hidden or self._has_static_hidden_details_activity(hidden_details))
            ):
                self._update_full_screen_static_min_summary()
            return

        for item in items:
            if item.kind == 'min_summary':
                if not self._replace_full_screen_static_min_summary(item):
                    self._print_static_transcript_renderable(item)
            else:
                self._print_static_transcript_renderable(item)
            self._reset_static_min_run_tracking()

    def show_inspectable_record(self, target: Dict[str, Any]) -> None:
        """Render one shared ``/show`` target at full detail without UI mutation."""

        message = target.get("message")
        if not isinstance(message, dict):
            raise ValueError("/show target is missing its source message")
        selected = dict(message)
        selected["msg_id"] = str(selected.get("id") or target.get("message_id") or "")
        selected["ts"] = selected.get("timestamp")
        record_id = str(target.get("record_id") or "")
        kind = str(target.get("kind") or "message")
        if kind == "tool_declaration":
            tool_call = target.get("tool_call")
            if not isinstance(tool_call, dict):
                raise ValueError("/show tool declaration is missing its tool call")
            selected["content"] = ""
            selected["reasoning"] = ""
            selected["tool_calls"] = [tool_call]
            selected["tool_stream"] = {}
            selected["tool_calls_stream"] = {}

        previous = getattr(self, "_display_verbosity", "min")
        self._display_verbosity = "max"
        try:
            token_stats = selected.get("token_stats")
            per_message_token_stats = None
            if isinstance(token_stats, dict):
                per_message_token_stats = {str(selected.get("msg_id") or ""): token_stats}
            items = self._static_transcript_message_renderables(
                selected,
                self._new_static_hidden_details_state(),
                per_message_token_stats=per_message_token_stats,
            )
        finally:
            self._display_verbosity = previous

        heading = f"/show {kind.replace('_', ' ')}: {record_id}"
        paired_ids = target.get("paired_message_ids")
        details = ["Full current-thread record; display verbosity is unchanged."]
        if selected.get("user_tool_call"):
            details.append("Origin: user tool call")
        if selected.get("incomplete"):
            details.append(f"Incomplete: {selected.get('incomplete_reason') or 'yes'}")
        if selected.get("runner_error"):
            details.append(f"Runner error: {selected.get('runner_error')}")
        if isinstance(paired_ids, list) and paired_ids:
            details.append(f"Exact paired message IDs: {', '.join(map(str, paired_ids))}")
        self.console_print_block(heading, "\n".join(details))
        for item in items:
            self._print_static_transcript_renderable(item)
        optimizer = selected.get("output_optimizer")
        if isinstance(optimizer, dict) and optimizer.get("optimized"):
            recovery = str(optimizer.get("summary_with_artifact") or optimizer.get("summary") or "").strip()
            raw_hint = str(optimizer.get("raw_hint") or "").strip()
            if raw_hint:
                recovery = f"{recovery}\nRaw output: {raw_hint}" if recovery else f"Raw output: {raw_hint}"
            if recovery:
                self.console_print_block("Output recovery", recovery)

    def flush_deferred_min_summary(self) -> bool:
        """Publish the aggregate for a watcher batch's completed hidden run."""

        return self._update_full_screen_static_min_summary()

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

    def redraw_static_view(
        self,
        *,
        reason: str = '',
        reuse_transcript_source: bool = False,
    ) -> None:
        """Clear terminal and reprint static transcript for current thread."""
        renderer = getattr(self, '_renderer', None)
        if self._is_full_screen_scrollback_renderer(renderer):
            source = None
            if reuse_transcript_source:
                source = self._coherent_transcript_scrollback_source(renderer)
            self._install_transcript_scrollback_source(
                renderer,
                reset_session_scrollback=True,
                repaint=True,
                source=source,
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
