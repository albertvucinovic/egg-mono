from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path
from eggllm import LLMClient
from .db import ThreadsDB
from .tools import ToolRegistry, create_default_tools
from .tool_state import ToolCallState, RunnerActionable, discover_runner_actionable, thread_state


# Use SQLite-compatible ISO format without 'T' to allow lexical comparisons in SQL queries
ISO = "%Y-%m-%d %H:%M:%S"


def _now_plus(ttl_sec: int) -> str:
    return (datetime.utcnow() + timedelta(seconds=ttl_sec)).strftime(ISO)


@dataclass
class RunnerConfig:
    lease_ttl_sec: int = 10
    heartbeat_sec: float = 1.0
    max_concurrent_threads: int = 4


class ThreadRunner:
    """Runs a single thread by acquiring the per-thread lease (open_streams with invoke_id fence)
    and streaming assistant output.
    """

    def __init__(self, db: ThreadsDB, thread_id: str, llm: Optional[LLMClient] = None, owner: Optional[str] = None, purpose: str = "assistant_stream", config: Optional[RunnerConfig] = None,
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None):
        self.db = db
        self.thread_id = thread_id
        self.llm = llm or LLMClient(models_path=models_path or "models.json", all_models_path=all_models_path or "all-models.json")
        self.owner = owner or os.environ.get("USER") or "runner"
        self.purpose = purpose
        self.cfg = config or RunnerConfig()
        self.tools = tools or create_default_tools()

    async def run_once(self) -> bool:
        """Attempt one assistant step (RA1/RA2/RA3) if runnable.

        Uses discover_runner_actionable() to decide what work to perform,
        acquires a lease with a fresh invoke_id, and records stream/open
        and stream/delta/stream/close events. Returns True if any work was
        performed, False if the thread was idle or paused.
        """
        # Respect paused threads
        th = self.db.get_thread(self.thread_id)
        if th and th.status == 'paused':
            return False

        # Determine what kind of work (if any) is pending
        ra = discover_runner_actionable(self.db, self.thread_id)
        if not ra:
            return False

        # Acquire lease with fresh invoke_id
        invoke_id = os.urandom(10).hex()
        lease_until = _now_plus(self.cfg.lease_ttl_sec)
        if not self.db.try_open_stream(self.thread_id, invoke_id, lease_until, owner=self.owner, purpose=self.purpose):
            return False

        # Resolve current model for visibility and to configure LLM
        try:
            current_model = getattr(self.llm, 'current_model_key', None)
        except Exception:
            current_model = None

        # Open streaming event tagged with model_key
        self.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=self.thread_id,
            type_='stream.open',
            msg_id=os.urandom(10).hex(),
            invoke_id=invoke_id,
            payload={'model_key': current_model},
        )

        # Heartbeat loop to keep lease alive
        stop_flag = False

        async def hb():
            nonlocal stop_flag
            while not stop_flag:
                await asyncio.sleep(self.cfg.heartbeat_sec)
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    stop_flag = True
                    return

        hb_task = asyncio.create_task(hb())

        # Shared helpers
        chunk_seq = self.db.max_chunk_seq(invoke_id)

        def _append_delta(payload: Dict[str, Any]):
            nonlocal chunk_seq
            chunk_seq += 1
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='stream.delta',
                invoke_id=invoke_id,
                chunk_seq=chunk_seq,
                payload=payload,
            )

        try:
            if ra.kind == 'RA1_llm':
                # ---------------- RA1: LLM call ----------------
                await self._run_ra1_llm(invoke_id, current_model)

            elif ra.kind in ('RA2_tools_assistant', 'RA3_tools_user'):
                # ---------------- RA2/RA3: tool calls ----------------
                # For now we do not stream tool execution output separately
                # via additional LLM calls; we simply execute tools for
                # approved tool calls and advance their states.
                await self._run_ra_tools(invoke_id, current_model, ra)

        except Exception as e:
            # Surface provider/config/network or tool errors into the thread
            try:
                err_payload = {'role': 'system', 'content': f'LLM/runner error: {e}'}
                if current_model:
                    err_payload['model_key'] = current_model
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=err_payload,
                )
                print(f"Runner error: {e}")
            except Exception:
                pass
        finally:
            try:
                hb_task.cancel()
            except Exception:
                pass

        # Close stream if we still own the lease
        try:
            row = self.db.current_open(self.thread_id)
            still_owner = bool(
                row
                and row['invoke_id'] == invoke_id
                and row['lease_until'] > datetime.utcnow().strftime(ISO)
            )
        except Exception:
            still_owner = False
        if still_owner:
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='stream.close',
                invoke_id=invoke_id,
                payload={},
            )

        # Rebuild snapshot and short_recap for readability
        try:
            cur = self.db.conn.execute(
                'SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC',
                (self.thread_id,),
            )
            evs = cur.fetchall()
            from .snapshot import SnapshotBuilder

            snap = SnapshotBuilder().build(evs)
            last_seq = evs[-1]['event_seq'] if evs else -1
            self.db.conn.execute(
                'UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?',
                (json.dumps(snap), last_seq, self.thread_id),
            )
            # Extract <short_recap>...</short_recap> from last assistant message
            try:
                def _extract_short(text: str) -> Optional[str]:
                    if not isinstance(text, str):
                        return None
                    end = text.rfind('</short_recap>')
                    if end == -1:
                        return None
                    start = text.rfind('<short_recap>', 0, end)
                    if start == -1:
                        return None
                    inner_start = start + len('<short_recap>')
                    if end < inner_start:
                        return None
                    return text[inner_start:end].strip()

                msgs = snap.get('messages', []) if isinstance(snap, dict) else []
                last_assist = None
                for m in reversed(msgs):
                    if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                        last_assist = m.get('content')
                        break
                rec = _extract_short(last_assist or '') if last_assist else None
                if rec:
                    self.db.conn.execute(
                        'UPDATE threads SET short_recap=? WHERE thread_id=?',
                        (rec, self.thread_id),
                    )
            except Exception:
                pass
        except Exception:
            pass

        # Attempt lease release (no-op if preempted)
        try:
            self.db.release(self.thread_id, invoke_id)
        except Exception:
            pass
        return True

    async def _run_ra1_llm(self, invoke_id: str, current_model: Optional[str]) -> None:
        """Handle RA1: perform a single LLM call, streaming deltas,
        and append the final assistant message with optional tool_calls.
        """
        from .tool_state import _last_stream_close_seq, _iter_messages_after

        # Re-discover the triggering message (first RA1-eligible message)
        last_close = _last_stream_close_seq(self.db, self.thread_id)
        trigger = None
        for ev in _iter_messages_after(self.db, self.thread_id, last_close):
            try:
                payload = json.loads(ev['payload_json']) if isinstance(ev['payload_json'], str) else (ev['payload_json'] or {})
            except Exception:
                payload = {}
            role = payload.get('role')
            keep_user_turn = bool(payload.get('keep_user_turn'))
            tool_calls = payload.get('tool_calls') or []
            if role == 'user' and not tool_calls and not keep_user_turn:
                trigger = (ev, payload)
                break
            if role == 'tool' and not keep_user_turn and not bool(payload.get('no_api')):
                trigger = (ev, payload)
                break
        if not trigger:
            return

        ev, payload = trigger
        role = payload.get('role')
        user_content = payload.get('content', '')

        # Build base_messages from snapshot, respecting no_api
        th = self.db.get_thread(self.thread_id)
        base_messages: List[Dict[str, Any]] = []
        if th and th.snapshot_json:
            try:
                snap = json.loads(th.snapshot_json)
                for m in snap.get('messages', []):
                    if m.get('no_api'):
                        continue
                    r = m.get('role')
                    content = m.get('content', '')
                    if r == 'assistant' and m.get('tool_calls'):
                        base_messages.append({'role': 'assistant', 'tool_calls': m.get('tool_calls')})
                    elif r == 'tool':
                        obj = {'role': 'tool', 'content': content}
                        if m.get('name'):
                            obj['name'] = m.get('name')
                        if m.get('tool_call_id'):
                            obj['tool_call_id'] = m.get('tool_call_id')
                        base_messages.append(obj)
                    elif r in ('system', 'user', 'assistant'):
                        base_messages.append({'role': r, 'content': content})
            except Exception:
                pass

        # Avoid duplicating trigger if already in snapshot
        try:
            last_seq = int(ev['event_seq'])
            snap_has_last = bool(th and isinstance(th.snapshot_last_event_seq, int) and th.snapshot_last_event_seq >= last_seq)
        except Exception:
            snap_has_last = False
        if not snap_has_last:
            if not payload.get('no_api'):
                if role == 'tool':
                    obj = {'role': 'tool', 'content': user_content}
                    if payload.get('name'):
                        obj['name'] = payload.get('name')
                    if payload.get('tool_call_id'):
                        obj['tool_call_id'] = payload.get('tool_call_id')
                    base_messages.append(obj)
                else:
                    base_messages.append({'role': 'user', 'content': user_content})

        # Apply last requested model change (same precedence as before)
        try:
            curm = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 200",
                (self.thread_id,),
            )
            model_key: Optional[str] = None
            for rr in curm.fetchall():
                try:
                    pj = json.loads(rr[0]) if isinstance(rr[0], str) else (rr[0] or {})
                except Exception:
                    pj = {}
                mk = pj.get('model_key')
                if isinstance(mk, str) and mk.strip():
                    model_key = mk.strip()
                    break
            if model_key:
                try:
                    self.llm.set_model(model_key)
                except Exception:
                    pass
            else:
                th2 = self.db.get_thread(self.thread_id)
                imk = getattr(th2, 'initial_model_key', None) if th2 else None
                if isinstance(imk, str) and imk.strip():
                    try:
                        self.llm.set_model(imk.strip())
                    except Exception:
                        pass
        except Exception:
            pass

        assistant_text_parts: List[str] = []
        reasoning_parts: List[str] = []

        recorder = None
        try:
            if os.environ.get('EGGTHREADS_RECORD_PROVIDER'):
                traces_dir = Path('.egg/traces')
                traces_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime('%Y%m%dT%H%M%S')
                rec_path = traces_dir / f"trace_{self.thread_id}_{ts}.jsonl"
                recorder = open(rec_path, 'a', encoding='utf-8')
        except Exception:
            recorder = None

        saw_content_delta = False
        saw_reason_delta = False
        chunk_seq = self.db.max_chunk_seq(invoke_id)

        try:
            async for raw in self.llm.astream_chat(base_messages, tools=self.tools.tools_spec() or None, tool_choice='auto'):
                try:
                    if recorder is not None:
                        recorder.write(json.dumps(raw, ensure_ascii=False) + "\n")
                        recorder.flush()
                except Exception:
                    pass
                if isinstance(raw, list):
                    evts = [e for e in raw if isinstance(e, dict)]
                elif isinstance(raw, dict):
                    evts = [raw]
                else:
                    continue
                for evt in evts:
                    et = evt.get('type')
                    if et == 'content_delta':
                        saw_content_delta = True
                        content = evt.get('text', '')
                        if content:
                            assistant_text_parts.append(content)
                            # Heartbeat / lease extension; stop if we lost lease
                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                return
                            chunk_seq += 1
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='stream.delta',
                                invoke_id=invoke_id,
                                chunk_seq=chunk_seq,
                                payload={'text': content, 'model_key': current_model},
                            )
                            await asyncio.sleep(0)
                    elif et == 'reasoning_delta':
                        saw_reason_delta = True
                        reason = evt.get('text', '')
                        if reason:
                            reasoning_parts.append(reason)
                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                return
                            chunk_seq += 1
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='stream.delta',
                                invoke_id=invoke_id,
                                chunk_seq=chunk_seq,
                                payload={'reason': reason, 'model_key': current_model},
                            )
                            await asyncio.sleep(0)
                    elif et == 'done':
                        final = evt.get('message') or {}
                        if not saw_content_delta:
                            fc = final.get('content')
                            if isinstance(fc, str) and fc:
                                assistant_text_parts = [fc]
                        if not saw_reason_delta:
                            fr = final.get('reasoning') or final.get('reason')
                            if isinstance(fr, str) and fr:
                                reasoning_parts = [fr]
                        assistant_msg: Dict[str, Any] = {'role': 'assistant'}
                        if assistant_text_parts:
                            assistant_msg['content'] = ''.join(assistant_text_parts)
                        tcs = final.get('tool_calls') or []
                        if isinstance(tcs, list) and tcs:
                            assistant_msg['tool_calls'] = tcs
                        if reasoning_parts:
                            assistant_msg['reasoning'] = ''.join(reasoning_parts)
                        if current_model:
                            assistant_msg['model_key'] = current_model
                        self.db.append_event(
                            event_id=os.urandom(10).hex(),
                            thread_id=self.thread_id,
                            type_='msg.create',
                            msg_id=os.urandom(10).hex(),
                            payload=assistant_msg,
                        )
                        return
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass


    async def _run_ra_tools(self, invoke_id: str, current_model: Optional[str], ra: RunnerActionable) -> None:
        """Handle RA2/RA3: process tool calls that are already approved or denied
        (TC2.1/TC2.2/TC5) and advance them along the state machine."""
        tool_calls = ra.tool_calls or []
        for tc in tool_calls:
            # Denied -> publish denial message and move to TC6
            if tc.state == 'TC2.2' and not tc.published:
                reason = 'Tool call execution denied.'
                msg = {
                    'role': 'tool',
                    'content': f"Tool call execution denied! Reason: {reason}",
                    'tool_call_id': tc.tool_call_id,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                if current_model:
                    msg['model_key'] = current_model
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=msg,
                )
                continue

            # Approved, not yet executed -> execution_started -> finished
            if tc.state == 'TC2.1':
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.execution_started',
                    msg_id=None,
                    invoke_id=invoke_id,
                    payload={'tool_call_id': tc.tool_call_id},
                )
                full_result = None
                try:
                    full_result = self.tools.execute(tc.name, tc.arguments)
                except Exception as e:
                    full_result = f"ERROR: {e}"
                if not isinstance(full_result, str):
                    full_result = str(full_result)
                out = full_result or ''
                CH = 400
                for i in range(0, len(out), CH):
                    part = out[i : i + CH]
                    self.db.append_event(
                        event_id=os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='stream.delta',
                        invoke_id=invoke_id,
                        chunk_seq=self.db.max_chunk_seq(invoke_id) + 1,
                        payload={'tool': {'name': tc.name or '', 'text': part, 'id': tc.tool_call_id}, 'model_key': current_model},
                    )
                    await asyncio.sleep(0)
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.finished',
                    msg_id=None,
                    invoke_id=invoke_id,
                    payload={'tool_call_id': tc.tool_call_id, 'reason': 'success'},
                )

            # Output approval done (TC5) -> publish final tool message
            if tc.state == 'TC5':
                msg = {
                    'role': 'tool',
                    'content': '',  # final content will be provided by UI via output_approval metadata in a later revision
                    'tool_call_id': tc.tool_call_id,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                if current_model:
                    msg['model_key'] = current_model
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=msg,
                )


class SubtreeScheduler:
    """Async orchestrator: watches a root thread and runs runnable threads within its subtree, up to concurrency limit."""

    def __init__(self, db: ThreadsDB, root_thread_id: str, llm: Optional[LLMClient] = None, owner: Optional[str] = None, config: Optional[RunnerConfig] = None,
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None):
        self.db = db
        self.root = root_thread_id
        self.llm = llm or LLMClient(models_path=models_path or "models.json", all_models_path=all_models_path or "all-models.json")
        print(f"LLMClient type: {type(self.llm)} module: {type(self.llm).__module__} has astream_chat: {hasattr(self.llm, 'astream_chat')}")
        self.owner = owner or os.environ.get("USER") or "scheduler"
        self.cfg = config or RunnerConfig()
        self.tools = tools or create_default_tools()

    def _collect_subtree(self, thread_id: str) -> List[str]:
        # BFS through children table
        out: List[str] = []
        q = [thread_id]
        seen = set()
        while q:
            t = q.pop(0)
            if t in seen:
                continue
            seen.add(t)
            # Respect waiting_until: only include children that are not waiting or waiting_until <= now
            out.append(t)
            cur = self.db.conn.execute("SELECT child_id, waiting_until FROM children WHERE parent_id=?", (t,))
            now_iso = datetime.utcnow().strftime(ISO)
            for row in cur.fetchall():
                wu = row["waiting_until"]
                if wu is None or wu <= now_iso:
                    q.append(row["child_id"])
        return out

    async def run_forever(self, poll_sec: float = 0.5):
        sem = asyncio.Semaphore(self.cfg.max_concurrent_threads)
        # Track currently running threads to avoid creating duplicate tasks
        running_threads = set()

        async def drive(tid: str):
            try:
                async with sem:
                    runner = ThreadRunner(self.db, tid, llm=self.llm, owner=self.owner, purpose="assistant_stream", config=self.cfg,
                                          tools=self.tools)
                    try:
                        await runner.run_once()
                    except Exception:
                        # swallow to keep loop alive; production would log event
                        pass
            finally:
                # Remove from running set when done
                if tid in running_threads:
                    running_threads.remove(tid)

        while True:
            # Only create tasks for threads that aren't already being processed AND are runnable
            for tid in self._collect_subtree(self.root):
                if tid not in running_threads:
                    # Check if thread is actually runnable before creating task
                    # This avoids creating tasks for threads that don't need processing
                    from .api import is_thread_runnable
                    if is_thread_runnable(self.db, tid):
                        running_threads.add(tid)
                        asyncio.create_task(drive(tid))
            await asyncio.sleep(poll_sec)
