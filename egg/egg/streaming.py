"""Streaming mixin for the egg application."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from eggthreads import create_snapshot, EventWatcher, ThreadsDB


# Per-source style for streaming content. Used by both the live delta
# dispatcher (``ingest_event_for_live``) and the replay path
# (``_replay_stream_to_renderer``) so styling decisions for each kind
# of provider output live in exactly one place.
STREAM_STYLE_TEXT: Optional[str] = None           # assistant content: plain
STREAM_STYLE_REASON: Optional[str] = "dim magenta"
STREAM_STYLE_TOOL_OUTPUT: Optional[str] = "yellow"
STREAM_STYLE_TOOL_CALL_ARGS: Optional[str] = "dim yellow"


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
            "tools": {},
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
                    await self.ingest_event_for_live(e, thread_id)
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
            for e in batch:
                try:
                    if e["type"] in ("msg.create", "msg.edit", "msg.delete"):
                        saw_non_stream_msg = True
                except Exception:
                    pass
                await self.ingest_event_for_live(e, thread_id)

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
                                self.console_print_message(m)
                            self._last_printed_seq_by_thread[self.current_thread] = ev_seq
                        except Exception:
                            pass
                except Exception:
                    pass

            # Recompute approval prompts when new events arrive so the
            # Approval panel content stays in sync, but avoid doing this on
            # every UI tick in update_panels(), which is costly on long
            # threads.
            try:
                self.compute_pending_prompt()
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
                "tools": {},
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
            tl = payload.get('tool')
            if isinstance(tl, dict):
                name = tl.get('name') or 'tool'
                tout = tl.get('text') or ''
                self._live_state.setdefault('tools', {})
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

    def _stream_append_on_renderer(self, text: str, *, style: Optional[str]) -> None:
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_append'):
            return
        # Escape Rich-markup brackets in raw provider content so it renders
        # literally (we don't know whether the provider's text happens to
        # look like markup tags).
        escaped = (text or "").replace('[', '\\[')
        payload = f"[{style}]{escaped}[/{style}]" if style else escaped
        try:
            renderer.stream_append(payload)
        except Exception:
            pass

    def _stream_end_on_renderer(self) -> None:
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not hasattr(renderer, 'stream_end'):
            return
        try:
            renderer.stream_end()
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
        for k in ls.get('tc_order') or []:
            t = (ls.get('tc_text') or {}).get(k, '')
            if isinstance(t, str) and t:
                self._stream_append_on_renderer(t, style=STREAM_STYLE_TOOL_CALL_ARGS)
