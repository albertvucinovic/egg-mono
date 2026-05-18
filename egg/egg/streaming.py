"""Streaming mixin for the egg application."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from eggthreads import create_snapshot, EventWatcher, ThreadsDB

from .panels import CHILDREN_PANEL_RELEVANT_EVENT_TYPES


# Per-source style for streaming content. Used by both the live delta
# dispatcher (``ingest_event_for_live``) and the replay path
# (``_replay_stream_to_renderer``) so styling decisions for each kind
# of provider output live in exactly one place.
STREAM_STYLE_TEXT: Optional[str] = None           # assistant content: plain
STREAM_STYLE_REASON: Optional[str] = "dim magenta"
STREAM_STYLE_REASONING_SUMMARY: Optional[str] = "dim magenta"
STREAM_STYLE_TOOL_OUTPUT: Optional[str] = None
STREAM_STYLE_TOOL_CALL_ARGS: Optional[str] = "dim yellow"
STREAM_STYLE_TOOL_SUMMARY: Optional[str] = "dim yellow"

# Streaming deltas can arrive much faster than a human-visible refresh rate,
# especially when attaching to an already-running reasoning stream. Repainting
# the full-screen renderer for every delta makes input/scrolling feel chunky, so
# coalesce renderer appends to a modest frame rate.
STREAM_RENDER_FLUSH_SEC = 0.05
STREAM_RENDER_MAX_BUFFER_CHARS = 64_000


def _new_tool_stream_indicator() -> Dict[str, Any]:
    return {"active": False, "name": "", "frames": 0}


def _new_tool_summary() -> Dict[str, Any]:
    return {"active": False, "name": "", "text": ""}


def _new_reasoning_summary() -> Dict[str, Any]:
    return {"active": False, "text": ""}


class StreamingMixin:
    """Mixin providing async streaming/watching methods for EggDisplayApp."""

    # Dedicated database connection for event watching to avoid contention
    # with the scheduler's write operations on the main db connection
    _watcher_db: Optional[ThreadsDB] = None

    def _get_watcher_db(self) -> ThreadsDB:
        """Get or create a dedicated database connection for event watching."""
        if self._watcher_db is None:
            # Use the same database path as the main connection
            db_path = getattr(self.db, 'path', '.egg/threads.sqlite')
            self._watcher_db = ThreadsDB(db_path)
        return self._watcher_db

    def _event_started_at_epoch(self, ts_value: Any) -> Optional[float]:
        """Parse an event/open timestamp into epoch seconds."""
        if not ts_value:
            return None
        s = str(ts_value)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return float(dt.timestamp())
            except Exception:
                continue
        return None

    async def start_watching_current(self):
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
            "stream_kind": None,
            "started_at": None,
            "content": "",
            "reason": "",
            "reasoning_summary": _new_reasoning_summary(),
            "tools": {},
            "tool_stream_indicator": _new_tool_stream_indicator(),
            "tool_summary": _new_tool_summary(),
            "tc_text": {},
            "tc_order": [],
        }
        # When switching to a thread, compute any pending tool or output
        # approvals once so that the Approval panel reflects existing
        # state even if no new events arrive. We deliberately avoid doing
        # this on every UI tick in update_panels() for performance.
        try:
            self.compute_pending_prompt()
        except Exception:
            pass
        self._watch_task = asyncio.create_task(self.watch_thread(self.current_thread))

    async def watch_thread(self, thread_id: str):
        try:
            self._mark_children_panel_dirty()
        except Exception:
            pass
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
                    "stream_kind": row_open["purpose"],
                    "started_at": self._event_started_at_epoch(row_open["opened_at"]),
                    "content": "",
                    "reason": "",
                    "reasoning_summary": _new_reasoning_summary(),
                    "tools": {},
                    "tool_stream_indicator": _new_tool_stream_indicator(),
                    "tool_summary": _new_tool_summary(),
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
                replay_rows = cur.fetchall()
                # When attaching to an already-running stream (common in
                # NO_API_CALLS/read-only viewer mode), there may be thousands of
                # reasoning deltas to replay. Yield periodically so the main UI
                # loop can keep processing keypresses, scroll events, and input
                # redraws while the stream buffer is being rebuilt.
                for idx, e in enumerate(replay_rows):
                    after_for_watch = e["event_seq"] or after_for_watch
                    await self.ingest_event_for_live(e, thread_id)
                    if idx and idx % 100 == 0:
                        await asyncio.sleep(0)
            except Exception:
                pass

        # Poll a bit less aggressively to reduce idle CPU; EventWatcher
        # itself backs off further when idle.
        # Use dedicated watcher_db connection to avoid SQLite contention
        # with the scheduler's write operations on the main db connection.
        watcher_db = self._get_watcher_db()
        ew = EventWatcher(watcher_db, thread_id, after_seq=after_for_watch, poll_sec=0.1)
        async for batch in ew.aiter():
            saw_non_stream_msg = False
            saw_compaction_marker = False
            saw_children_status_event = False
            saw_approval_event = False
            for idx, e in enumerate(batch):
                try:
                    event_type = e["type"]
                    if event_type in ("msg.create", "msg.edit", "msg.delete"):
                        saw_non_stream_msg = True
                    elif event_type == "thread.compaction":
                        saw_compaction_marker = True
                    elif event_type in ("tool_call.approval", "tool_call.output_approval"):
                        saw_approval_event = True
                    if event_type in CHILDREN_PANEL_RELEVANT_EVENT_TYPES:
                        saw_children_status_event = True
                except Exception:
                    pass
                await self.ingest_event_for_live(e, thread_id)
                # Same fairness rule for live catch-up batches: a big burst of
                # reasoning deltas should not monopolize the event loop and make
                # typing/scrolling appear frozen.
                try:
                    if idx and idx % 100 == 0:
                        await asyncio.sleep(0)
                except Exception:
                    pass

            if saw_children_status_event:
                try:
                    self._mark_children_panel_dirty()
                except Exception:
                    pass

            # If we saw message-level events or compaction markers, refresh the
            # snapshot/console transcript.  Compaction markers are non-message
            # control events, so the UI must explicitly render them without
            # hiding the surrounding messages.
            if saw_non_stream_msg or saw_compaction_marker:
                try:
                    create_snapshot(self.db, self.current_thread)
                except Exception:
                    pass
                # Print any new messages and compaction dividers to console
                # (above live panel), preserving event order.
                try:
                    last_printed = self._last_printed_seq_by_thread.get(self.current_thread, -1)
                    cur = self.db.conn.execute(
                        "SELECT event_seq, type, msg_id, ts, payload_json FROM events "
                        "WHERE thread_id=? AND event_seq>? AND type IN ('msg.create', 'thread.compaction') "
                        "ORDER BY event_seq ASC",
                        (self.current_thread, last_printed)
                    )
                    rows = cur.fetchall()
                    for row in rows:
                        try:
                            ev_seq = int(row['event_seq'])
                            typ = row['type']
                            msg_id = row['msg_id']
                            ts = row['ts']
                            pj = row['payload_json']
                            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
                            if typ == 'thread.compaction':
                                marker = dict(payload) if isinstance(payload, dict) else {}
                                marker.setdefault('event_seq', ev_seq)
                                if ts is not None:
                                    marker.setdefault('ts', ts)
                                self.console_print_compaction_marker(marker)
                            elif isinstance(payload, dict):
                                # Ensure msg_id, ts, and event_seq are propagated so
                                # console titles can display them.
                                payload.setdefault('msg_id', msg_id)
                                payload.setdefault('event_seq', ev_seq)
                                if ts is not None:
                                    payload.setdefault('ts', ts)
                                self.console_print_message(payload)
                            self._last_printed_seq_by_thread[self.current_thread] = ev_seq
                        except Exception:
                            pass
                except Exception:
                    pass

            # Recompute approval prompts when user-actionable events arrive so the
            # Approval panel content stays in sync, but avoid doing this on
            # every UI tick in update_panels(), which is costly on long
            # threads. Tool stream.delta bursts can arrive very frequently;
            # approval state cannot change on those preview-only events, so
            # don't rescan the tool-call state for each streaming chunk.
            if saw_non_stream_msg or saw_compaction_marker or saw_approval_event:
                try:
                    self.compute_pending_prompt()
                except Exception:
                    pass

            # EventWatcher immediately yields again while rows are available.
            # Tool streams can therefore arrive as many small batches, and the
            # per-batch loop above would otherwise monopolize the asyncio loop
            # even though each individual batch is tiny. Yield once per batch
            # so input echo and in-app scroll handling stay responsive during
            # high-frequency tool output.
            try:
                await asyncio.sleep(0)
            except Exception:
                pass

    async def ingest_event_for_live(self, e, thread_id: str):
        if thread_id != self.current_thread:
            return
        t = e["type"]
        if t == 'stream.open':
            started_at = self._event_started_at_epoch(e["ts"] if "ts" in e.keys() else None)
            if started_at is None:
                try:
                    started_at = float(time.time())
                except Exception:
                    started_at = None
            stream_kind = None
            try:
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                sk = payload.get('stream_kind') or payload.get('purpose')
                if isinstance(sk, str) and sk:
                    stream_kind = sk
            if not stream_kind:
                try:
                    stream_kind = e.get('purpose') if isinstance(e, dict) else None
                except Exception:
                    stream_kind = None
            self._live_state = {
                "active_invoke": e["invoke_id"],
                "stream_kind": stream_kind,
                "started_at": started_at,
                "content": "",
                "reason": "",
                "reasoning_summary": _new_reasoning_summary(),
                "tools": {},
                "tool_stream_indicator": _new_tool_stream_indicator(),
                "tool_summary": _new_tool_summary(),
                "tc_text": {},
                "tc_order": [],
            }
            try:
                inv = e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]
                self.log_system(f"Streaming started (invoke {str(inv)[-6:]}).")
            except Exception:
                pass
            self._stream_begin_on_renderer(stream_kind)
        elif t == 'stream.delta':
            try:
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
            except Exception:
                payload = {}
            txt = payload.get('text') or payload.get('content') or payload.get('delta')
            if isinstance(txt, str) and txt:
                self._live_state['content'] = (self._live_state.get('content') or '') + txt
                self._stream_append_on_renderer(txt, style=STREAM_STYLE_TEXT)
            rs = payload.get('reason')
            if isinstance(rs, str) and rs:
                self._live_state['reason'] = (self._live_state.get('reason') or '') + rs
                self._stream_append_on_renderer(rs, style=STREAM_STYLE_REASON)
            rsum = payload.get('reasoning_summary')
            if isinstance(rsum, str) and rsum:
                summary_state = self._live_state.setdefault('reasoning_summary', _new_reasoning_summary())
                summary_state['active'] = True
                summary_state['text'] = str(summary_state.get('text') or '') + rsum
                self._stream_append_on_renderer(rsum, style=STREAM_STYLE_REASONING_SUMMARY)
            tl = payload.get('tool')
            if isinstance(tl, dict):
                name = tl.get('name') or 'tool'
                tout = tl.get('text') or ''
                self._live_state.setdefault('tools', {})
                is_suppressed = bool(tl.get('suppressed'))
                if is_suppressed:
                    indicator = self._live_state.setdefault('tool_stream_indicator', _new_tool_stream_indicator())
                    indicator['active'] = True
                    indicator['name'] = name
                    indicator['frames'] = int(indicator.get('frames') or 0) + 1
                    if name not in self._live_state['tools']:
                        self._live_state['tools'][name] = self._live_state['tools'].get(name, '')
                else:
                    self._live_state['tools'][name] = self._live_state['tools'].get(name, '') + tout
                    if tout:
                        self._stream_append_on_renderer(tout, style=STREAM_STYLE_TOOL_OUTPUT)
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
                    self._stream_append_on_renderer(frag, style=STREAM_STYLE_TOOL_CALL_ARGS)
        elif t == 'tool_call.summary':
            try:
                payload = json.loads(e['payload_json']) if isinstance(e['payload_json'], str) else (e['payload_json'] or {})
            except Exception:
                payload = {}
            summary = payload.get('summary') if isinstance(payload, dict) else None
            if isinstance(summary, str) and summary:
                tsummary = self._live_state.setdefault('tool_summary', _new_tool_summary())
                tsummary['active'] = True
                tsummary['name'] = str(payload.get('name') or tsummary.get('name') or 'tool')
                tsummary['text'] = summary
        elif t == 'stream.close':
            self._live_state['active_invoke'] = None
            self._live_state['stream_kind'] = None
            self._live_state['started_at'] = None
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
            self._stream_end_on_renderer()
            self.log_system('Streaming finished.')

    def _stream_begin_on_renderer(self, stream_kind: Optional[str]) -> None:
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_begin'):
            return
        try:
            self._clear_stream_render_buffer()
            renderer.stream_begin()
            kind = (stream_kind or 'stream').lower()
            if kind == 'llm':
                header = "[dim cyan]── Assistant (streaming) ──[/dim cyan]\n"
            elif kind == 'tool':
                header = "[dim yellow]── Tool (streaming) ──[/dim yellow]\n"
            else:
                header = f"[dim]── {kind} (streaming) ──[/dim]\n"
            renderer.stream_append(header)
        except Exception:
            pass

    def _stream_payload_markup(self, text: str, *, style: Optional[str]) -> str:
        # Escape Rich-markup brackets in raw provider content so it renders
        # literally (we don't know whether the provider's text happens to
        # look like markup tags).
        if not style:
            return text or ""
        escaped = (text or "").replace('[', '\\[')
        return f"[{style}]{escaped}[/{style}]" if style else escaped

    def _clear_stream_render_buffer(self) -> None:
        try:
            task = getattr(self, '_stream_render_flush_task', None)
            if task is not None:
                task.cancel()
        except Exception:
            pass
        self._stream_render_flush_task = None
        self._stream_render_buffer = []
        self._stream_render_buffer_chars = 0

    async def _delayed_stream_render_flush(self) -> None:
        try:
            await asyncio.sleep(STREAM_RENDER_FLUSH_SEC)
            self._stream_render_flush_task = None
            self._flush_stream_render_buffer_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stream_render_flush_task = None

    def _schedule_stream_render_flush(self) -> None:
        try:
            task = getattr(self, '_stream_render_flush_task', None)
            if task is not None and not task.done():
                return
            loop = asyncio.get_running_loop()
            self._stream_render_flush_task = loop.create_task(self._delayed_stream_render_flush())
        except Exception:
            # If there is no running loop (e.g. an isolated unit helper), keep
            # semantics simple and render immediately.
            self._flush_stream_render_buffer_now()

    def _flush_stream_render_buffer_now(self, *, force: bool = False) -> None:
        buf = list(getattr(self, '_stream_render_buffer', []) or [])
        if not buf:
            return
        if not force:
            try:
                q = getattr(getattr(self.input_panel, 'editor', None), 'input_queue', None)
                input_pending = q is not None and hasattr(q, 'empty') and not q.empty()
            except Exception:
                input_pending = False
            try:
                input_dirty = (
                    getattr(self.input_panel, '_cached_render', None) is not None
                    and bool(self.input_panel.is_dirty())
                )
            except Exception:
                input_dirty = False
            buffer_is_bounded = int(getattr(self, '_stream_render_buffer_chars', 0) or 0) < STREAM_RENDER_MAX_BUFFER_CHARS
            if (input_pending or input_dirty) and buffer_is_bounded:
                try:
                    asyncio.get_running_loop()
                except Exception:
                    pass
                else:
                    self._schedule_stream_render_flush()
                    return
        self._stream_render_buffer = []
        self._stream_render_buffer_chars = 0
        try:
            task = getattr(self, '_stream_render_flush_task', None)
            if task is not None and not task.done():
                task.cancel()
        except Exception:
            pass
        self._stream_render_flush_task = None

        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_append'):
            return
        try:
            payload = ''.join(
                self._stream_payload_markup(text, style=style)
                for text, style in buf
                if isinstance(text, str) and text
            )
            if payload:
                renderer.stream_append(payload)
        except Exception:
            pass

    def _stream_append_on_renderer(self, text: str, *, style: Optional[str]) -> None:
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_append'):
            return
        try:
            if not isinstance(text, str) or not text:
                return
            buf = getattr(self, '_stream_render_buffer', None)
            if not isinstance(buf, list):
                buf = []
                self._stream_render_buffer = buf
            buf.append((text, style))
            self._stream_render_buffer_chars = int(getattr(self, '_stream_render_buffer_chars', 0) or 0) + len(text)
            # Bound attach-time buffers so a very large replay does not defer a
            # huge render until the end. Normal live streaming is flushed by the
            # short timer below.
            if self._stream_render_buffer_chars >= STREAM_RENDER_MAX_BUFFER_CHARS:
                self._flush_stream_render_buffer_now()
            else:
                self._schedule_stream_render_flush()
        except Exception:
            pass

    def _tool_stream_indicator_text(self, *, name: str = "", frames: int = 0, compact: bool = False) -> str:
        frames_list = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        try:
            glyph = frames_list[int(frames or 0) % len(frames_list)]
        except Exception:
            glyph = "…"
        if compact:
            suffix = f" {name}" if name else ""
            return f"{glyph} tool{suffix}: saving output"
        suffix = f" ({name})" if name else ""
        return f"{glyph} tool streaming{suffix}: preview limit reached; saving output only"

    def _stream_end_on_renderer(self) -> None:
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_end'):
            return
        try:
            self._flush_stream_render_buffer_now(force=True)
            renderer.stream_end()
            self._clear_stream_render_buffer()
        except Exception:
            pass

    def _replay_stream_to_renderer(self) -> None:
        """Re-seed the renderer's stream buffer from accumulated _live_state.

        Used after a display-mode switch mid-stream: the new renderer
        has an empty stream buffer but ``_live_state`` still holds the
        content that has been accumulated so far. Re-emitting it lets
        the in-flight preview pick up seamlessly on the new surface.

        No-op when no stream is active, or when the renderer doesn't
        support the stream API (inline mode — compose_chat_panel_text
        reads ``_live_state`` directly and shows it in the chat panel).
        """
        ls = getattr(self, '_live_state', None) or {}
        if not ls.get('active_invoke'):
            return
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_begin'):
            return
        self._stream_begin_on_renderer(ls.get('stream_kind'))
        reason = ls.get('reason') or ''
        if isinstance(reason, str) and reason:
            self._stream_append_on_renderer(reason, style=STREAM_STYLE_REASON)
        content = ls.get('content') or ''
        if isinstance(content, str) and content:
            self._stream_append_on_renderer(content, style=STREAM_STYLE_TEXT)
        for name, txt in (ls.get('tools') or {}).items():
            if isinstance(txt, str) and txt:
                self._stream_append_on_renderer(txt, style=STREAM_STYLE_TOOL_OUTPUT)
        indicator = ls.get('tool_stream_indicator') or {}
        for k in ls.get('tc_order') or []:
            t = (ls.get('tc_text') or {}).get(k, '')
            if isinstance(t, str) and t:
                self._stream_append_on_renderer(t, style=STREAM_STYLE_TOOL_CALL_ARGS)
        self._flush_stream_render_buffer_now(force=True)
