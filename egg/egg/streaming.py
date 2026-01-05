"""Streaming mixin for the egg application."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from eggthreads import create_snapshot, EventWatcher


class StreamingMixin:
    """Mixin providing async streaming/watching methods for EggDisplayApp."""

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
        ew = EventWatcher(self.db, thread_id, after_seq=after_for_watch, poll_sec=0.1)
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
            self._live_state = {"active_invoke": e["invoke_id"], "content": "", "reason": "", "tools": {}, "tc_text": {}, "tc_order": []}
            try:
                inv = e.get("invoke_id") if isinstance(e, dict) else e["invoke_id"]
                self.log_system(f"Streaming started (invoke {str(inv)[-6:]}).")
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
            self.log_system('Streaming finished.')
