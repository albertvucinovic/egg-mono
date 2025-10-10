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
        """Attempt one assistant step: if runnable, acquire lease with a fresh invoke_id, stream deltas as events, close, release.
        Return True if any work was done.
        """
        # Decide if there is pending user message to answer by scanning snapshot or events.
        # Minimal heuristic for now: if last event is a user msg.create, produce one assistant response.
        # Respect paused threads
        th = self.db.get_thread(self.thread_id)
        if th and th.status == 'paused':
            return False

        # Find last trigger message (user, tool, or assistant with tool_calls);
        # run if it is after the last stream.close
        # Find the earliest such message strictly after the last stream.close
        last_close_row = self.db.conn.execute(
            "SELECT MAX(event_seq) AS max_close FROM events WHERE thread_id=? AND type='stream.close'",
            (self.thread_id,)
        ).fetchone()
        last_close_seq = int(last_close_row[0]) if last_close_row and last_close_row[0] is not None else -1
        row_user = self.db.conn.execute(
            """
            SELECT e.* FROM events e
             WHERE e.thread_id=?
               AND e.event_seq>? 
               AND e.type='msg.create'
               AND (
                    json_extract(e.payload_json,'$.role')='user'
                 OR json_extract(e.payload_json,'$.role')='tool'
                 OR (
                      json_extract(e.payload_json,'$.role')='assistant'
                  AND json_extract(e.payload_json,'$.tool_calls') IS NOT NULL
                    )
               )
             ORDER BY e.event_seq ASC LIMIT 1
            """,
            (self.thread_id, last_close_seq)
        ).fetchone()
        if not row_user:
            return False
        need_answer = row_user
        try:
            pl = json.loads(need_answer["payload_json"]) if isinstance(need_answer["payload_json"], str) else (need_answer["payload_json"] or {})
        except Exception:
            pl = {}
        user_content = pl.get("content", "")
        role_of_trigger = pl.get("role")

        # Prepare conversation: reconstruct messages from snapshot + maybe the latest trigger content
        # Minimal: fetch snapshot if present
        th = self.db.get_thread(self.thread_id)
        base_messages: List[Dict[str, Any]] = []
        if th and th.snapshot_json:
            try:
                snap = json.loads(th.snapshot_json)
                for m in snap.get("messages", []):
                    role = m.get("role")
                    content = m.get("content", "")
                    if role == "assistant" and m.get("tool_calls"):
                        base_messages.append({"role": "assistant", "tool_calls": m.get("tool_calls")})
                    elif role == "tool":
                        # Preserve tool tool_call_id if present
                        obj = {"role": "tool", "content": content}
                        if m.get("name"):
                            obj["name"] = m.get("name")
                        if m.get("tool_call_id"):
                            obj["tool_call_id"] = m.get("tool_call_id")
                        base_messages.append(obj)
                    elif role in ("system", "user", "assistant"):
                        base_messages.append({"role": role, "content": content})
            except Exception:
                pass
        # Avoid duplicating the last trigger message if already in snapshot
        try:
            last_user_seq = int(need_answer["event_seq"]) if need_answer is not None else -1
            snap_has_last = bool(th and isinstance(th.snapshot_last_event_seq, int) and th.snapshot_last_event_seq >= last_user_seq)
        except Exception:
            snap_has_last = False
        if not snap_has_last:
            if role_of_trigger == "assistant" and isinstance(pl.get("tool_calls"), list):
                base_messages.append({"role": "assistant", "tool_calls": pl.get("tool_calls")})
            elif role_of_trigger == "tool":
                obj = {"role": "tool", "content": user_content}
                if pl.get("name"):
                    obj["name"] = pl.get("name")
                if pl.get("tool_call_id"):
                    obj["tool_call_id"] = pl.get("tool_call_id")
                base_messages.append(obj)
            else:
                base_messages.append({"role": "user", "content": user_content})

        # Apply last requested model change (tracked via msg.create with payload.model_key)
        try:
            curm = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 200",
                (self.thread_id,)
            )
            model_key: Optional[str] = None
            for rr in curm.fetchall():
                try:
                    pj = json.loads(rr[0]) if isinstance(rr[0], str) else (rr[0] or {})
                except Exception:
                    pj = {}
                mk = pj.get("model_key")
                if isinstance(mk, str) and mk.strip():
                    model_key = mk.strip()
                    break
            if model_key:
                try:
                    self.llm.set_model(model_key)
                except Exception:
                    pass
        except Exception:
            pass

        # Acquire lease with fresh invoke_id
        invoke_id = os.urandom(10).hex()
        lease_until = _now_plus(self.cfg.lease_ttl_sec)
        if not self.db.try_open_stream(self.thread_id, invoke_id, lease_until, owner=self.owner, purpose=self.purpose):
            return False

        # Open streaming event; tag with current model for UI visibility
        try:
            current_model = getattr(self.llm, 'current_model_key', None)
        except Exception:
            current_model = None
        self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.open",
                             msg_id=os.urandom(10).hex(), invoke_id=invoke_id, payload={"model_key": current_model})
        current_invoke = invoke_id

        # Background heartbeat
        stop_flag = False

        async def hb():
            nonlocal stop_flag
            while not stop_flag:
                await asyncio.sleep(self.cfg.heartbeat_sec)
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    # lost lease -> stop
                    stop_flag = True
                    return

        hb_task = asyncio.create_task(hb())

        # Stream from LLM (single turn)
        chunk_seq = self.db.max_chunk_seq(invoke_id)
        # Optional provider event recording for debugging (set EGGTHREADS_RECORD_PROVIDER=1)
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

        try:
            assistant_text_parts: List[str] = []
            reasoning_parts: List[str] = []
            tool_calls_buf: Dict[int, Any] = {}
            completed_with_tools = False
            pending_assistant_msg: Optional[Dict[str, Any]] = None
            pending_tool_msgs: List[Dict[str, Any]] = []
            saw_content_delta = False
            saw_reason_delta = False
            async for raw in self.llm.astream_chat(base_messages, tools=self.tools.tools_spec() or None, tool_choice="auto"):
                    # Record raw provider event(s)
                    try:
                        if recorder is not None:
                            recorder.write(json.dumps(raw, ensure_ascii=False) + "\n")
                            recorder.flush()
                    except Exception:
                        pass
                    # Providers may yield a single dict or a list of dict events
                    if isinstance(raw, list):
                        evts = [e for e in raw if isinstance(e, dict)]
                    elif isinstance(raw, dict):
                        evts = [raw]
                    else:
                        continue
                    for evt in evts:
                        if stop_flag:
                            break
                        if evt.get("type") == "content_delta":
                            saw_content_delta = True
                            content = evt.get("text", "")
                            if content:
                                assistant_text_parts.append(content)
                            chunk_seq += 1
                            ok = self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec))
                            if not ok:
                                stop_flag = True
                                break
                            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                 invoke_id=invoke_id, chunk_seq=chunk_seq, payload={"text": content, "model_key": current_model})
                            # Yield to event loop so UIs can pick up deltas in near real-time
                            await asyncio.sleep(0)  # yield to event loop
                        elif evt.get("type") == "reasoning_delta":
                            saw_reason_delta = True
                            reason = evt.get("text", "")
                            if reason:
                                reasoning_parts.append(reason)
                            chunk_seq += 1
                            ok = self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec))
                            if not ok:
                                stop_flag = True
                                break
                            # Store reasoning under a dedicated key so snapshot can incorporate
                            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                 invoke_id=invoke_id, chunk_seq=chunk_seq, payload={"reason": reason, "model_key": current_model})
                            await asyncio.sleep(0)
                        elif evt.get("type") in (
                            "tool_calls_delta",
                            "tool_call_delta",
                            "tool_call_arguments_delta",
                            "function_call_delta",
                            "function_call_arguments_delta",
                        ):
                            # Stream tool-call arguments deltas (provider-specific shapes). Do not execute tools here.
                            # Recursively collect any 'tool_calls' arrays from the event (handles nested choices[].delta.tool_calls etc.)
                            def _collect_tool_calls(node, acc: List[Dict[str, Any]]):
                                if isinstance(node, dict):
                                    for k, v in node.items():
                                        if k == 'tool_calls' and isinstance(v, list):
                                            for item in v:
                                                if isinstance(item, dict):
                                                    acc.append(item)
                                        else:
                                            _collect_tool_calls(v, acc)
                                elif isinstance(node, list):
                                    for it in node:
                                        _collect_tool_calls(it, acc)

                            calls_list: List[Dict[str, Any]] = []
                            _collect_tool_calls(evt, calls_list)
                            if not calls_list:
                                # Fallback single-call shapes
                                if isinstance(evt.get("function"), dict):
                                    calls_list = [{"function": evt.get("function")}]  
                                elif evt.get("name") or evt.get("arguments_delta") or evt.get("arguments"):
                                    calls_list = [{"function": {"name": evt.get("name"), "arguments_delta": evt.get("arguments_delta") or evt.get("arguments")}}]
                            for idx, c in enumerate(calls_list or []):
                                fn = (c or {}).get("function") or {}
                                name = fn.get("name") or c.get("name") or ""
                                tcid = c.get("id") or c.get("index") or fn.get("id") or idx
                                arg_delta = fn.get("arguments_delta") or fn.get("arguments") or c.get("arguments_delta") or c.get("arguments") or ""
                                if isinstance(arg_delta, (dict, list)):
                                    try:
                                        arg_delta = json.dumps(arg_delta, ensure_ascii=False)
                                    except Exception:
                                        arg_delta = str(arg_delta)
                                if not arg_delta:
                                    continue
                                chunk_seq += 1
                                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                    stop_flag = True
                                    break
                                payload = {"tool_call": {"name": name, "text": str(arg_delta), "id": str(tcid)}, "model_key": current_model}
                                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                     invoke_id=invoke_id, chunk_seq=chunk_seq, payload=payload)
                            await asyncio.sleep(0)
                        elif evt.get("type") == "done":  # end of provider stream call
                            final = evt.get("message") or {}
                            # If provider only supplied final content at done, emit content in small chunks for visible streaming
                            try:
                                if not saw_content_delta:
                                    fc = final.get("content")
                                    if isinstance(fc, str) and fc:
                                        chunks = [fc[i:i+40] for i in range(0, len(fc), 40)]
                                        for ch in chunks:
                                            chunk_seq += 1
                                            ok = self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec))
                                            if not ok:
                                                stop_flag = True
                                                break
                                            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                                 invoke_id=invoke_id, chunk_seq=chunk_seq, payload={"text": ch, "model_key": current_model})
                                        await asyncio.sleep(0.02)
                            except Exception:
                                pass
                            # If provider never streamed reasoning but final contains it, stream that too
                            try:
                                if not saw_reason_delta:
                                    fr = final.get("reasoning") or final.get("reason")
                                    if isinstance(fr, str) and fr:
                                        chunks = [fr[i:i+60] for i in range(0, len(fr), 60)]
                                        for ch in chunks:
                                            chunk_seq += 1
                                            ok = self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec))
                                            if not ok:
                                                stop_flag = True
                                                break
                                            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                                 invoke_id=invoke_id, chunk_seq=chunk_seq, payload={"reason": ch, "model_key": current_model})
                                        await asyncio.sleep(0.02)
                            except Exception:
                                pass
                            # Handle tool calls if present (exclusive mode under same lease). First, stream final tool-call arguments if we haven't already.
                            tcs = final.get("tool_calls") or []
                            had_tools = bool(tcs)
                            for idx, tc in enumerate(tcs):
                                f = (tc or {}).get("function") or {}
                                name = f.get("name") or ""
                                args_full = f.get("arguments")
                                tc_id_final = (tc or {}).get("id")
                                # Normalize arguments to string for streaming
                                if isinstance(args_full, (dict, list)):
                                    try:
                                        args_str = json.dumps(args_full, ensure_ascii=False)
                                    except Exception:
                                        args_str = str(args_full)
                                elif isinstance(args_full, str):
                                    args_str = args_full
                                else:
                                    args_str = str(args_full or "")
                                # stream arguments in chunks for UI visibility
                                try:
                                    if args_str:
                                        for i in range(0, len(args_str), 200):
                                            part = args_str[i:i+200]
                                            chunk_seq += 1
                                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                                stop_flag = True
                                                break
                                            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                                 invoke_id=invoke_id, chunk_seq=chunk_seq, payload={"tool_call": {"name": name, "id": str(tc_id_final or idx), "text": part}, "model_key": current_model})
                                            await asyncio.sleep(0)
                                except Exception:
                                    pass
                            # Then execute tools and stream their outputs.
                            tool_messages = []
                            for idx, tc in enumerate(tcs):
                                f = (tc or {}).get("function") or {}
                                name = f.get("name")
                                args = f.get("arguments")
                                call_id = (tc or {}).get("id")
                                full_result = None
                                try:
                                    result = self.tools.execute(name, args)
                                    full_result = str(result)
                                except Exception as e:
                                    full_result = f"ERROR: {e}"
                                try:
                                    out = full_result or ""
                                    CH = 400
                                    for i in range(0, len(out), CH):
                                        part = out[i:i+CH]
                                        chunk_seq += 1
                                        ok = self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec))
                                        if not ok:
                                            stop_flag = True
                                            break
                                        payload = {"tool": {"name": name or "", "text": part, "id": str(call_id or idx)}, "model_key": current_model}
                                        self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.delta",
                                                             invoke_id=invoke_id, chunk_seq=chunk_seq, payload=payload)
                                        await asyncio.sleep(0)
                                except Exception:
                                    pass
                                # Accumulate tool result message; we will persist after stream.close
                                tool_messages.append({"role": "tool", "name": name, "content": full_result or "", "tool_call_id": str(call_id or idx)})
                            if had_tools:
                                # Prepare to append assistant+tool messages AFTER stream.close
                                assistant_msg = {"role": "assistant"}
                                if assistant_text_parts:
                                    assistant_msg["content"] = "".join(assistant_text_parts)
                                if isinstance(final.get("tool_calls"), list) and final.get("tool_calls"):
                                    assistant_msg["tool_calls"] = final.get("tool_calls")
                                if reasoning_parts:
                                    assistant_msg["reasoning"] = "".join(reasoning_parts)
                                if current_model:
                                    assistant_msg["model_key"] = current_model
                                pending_assistant_msg = assistant_msg
                                pending_tool_msgs = tool_messages[:]
                                completed_with_tools = True
                            # End of stream loop
                            break
        except Exception as e:
            # Surface provider/config/network errors into the thread
            try:
                err_payload = {"role": "system", "content": f"LLM error: {e}"}
                try:
                    current_model = getattr(self.llm, 'current_model_key', None)
                    if current_model:
                        err_payload["model_key"] = current_model
                except Exception:
                    pass
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload=err_payload)
                print(f"LLM error: {e}")
            except Exception:
                pass
        finally:
            try:
                hb_task.cancel()
            except Exception:
                pass
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass

        # Close and release only if we still hold the lease
        try:
            row = self.db.current_open(self.thread_id)
            still_owner = bool(row and row["invoke_id"] == invoke_id and row["lease_until"] > datetime.utcnow().strftime(ISO))
        except Exception:
            still_owner = False
        if still_owner:
            self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_="stream.close",
                                 invoke_id=invoke_id, payload={})
        # If we executed tools, append pending assistant+tool messages now (after close), so next run can trigger
        if completed_with_tools:
            try:
                # Append the assistant message with tool_calls first (must precede tool messages)
                if 'pending_assistant_msg' in locals() and pending_assistant_msg:
                    self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_='msg.create',
                                         msg_id=os.urandom(10).hex(), payload=pending_assistant_msg)
                # Then append tool messages (tool_call_id must refer to above calls)
                if 'pending_tool_msgs' in locals():
                    for tm in pending_tool_msgs:
                        self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_='msg.create',
                                             msg_id=os.urandom(10).hex(), payload=tm)
            except Exception:
                pass
        # If we didn't execute tools, append final assistant message now
        if not completed_with_tools:
            assistant_msg = {"role": "assistant"}
            if assistant_text_parts:
                assistant_msg["content"] = "".join(assistant_text_parts)
            if reasoning_parts:
                assistant_msg["reasoning"] = "".join(reasoning_parts)
            # Append to db
            try:
                if current_model:
                    assistant_msg["model_key"] = current_model
                self.db.append_event(event_id=os.urandom(10).hex(), thread_id=self.thread_id, type_='msg.create',
                                     msg_id=os.urandom(10).hex(), payload=assistant_msg)
            except Exception:
                pass
        # Update snapshot for readability and update short_recap if present in last assistant message
        try:
            cur = self.db.conn.execute("SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC", (self.thread_id,))
            evs = cur.fetchall()
            from .snapshot import SnapshotBuilder
            snap = SnapshotBuilder().build(evs)
            last_seq = evs[-1]["event_seq"] if evs else -1
            self.db.conn.execute("UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?",
                                (json.dumps(snap), last_seq, self.thread_id))
            # Extract <short_recap>...</short_recap> from last assistant message
            try:
                def _extract_short(text: str) -> Optional[str]:
                    if not isinstance(text, str):
                        return None
                    start = text.find('<short_recap>')
                    end = text.find('</short_recap>')
                    if start != -1 and end != -1 and end > start:
                        return text[start+13:end].strip()
                    return None
                msgs = snap.get("messages", []) if isinstance(snap, dict) else []
                last_assist = None
                for m in reversed(msgs):
                    if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                        last_assist = m.get('content')
                        break
                rec = _extract_short(last_assist or '') if last_assist else None
                if rec:
                    self.db.conn.execute("UPDATE threads SET short_recap=? WHERE thread_id=?", (rec, self.thread_id))
            except Exception:
                pass
        except Exception:
            pass
        # Attempt release (will no-op if preempted)
        try:
            self.db.release(self.thread_id, invoke_id)
        except Exception:
            pass
        return True


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
                    if self._is_thread_runnable(tid):
                        running_threads.add(tid)
                        asyncio.create_task(drive(tid))
            await asyncio.sleep(poll_sec)

    def _is_thread_runnable(self, tid: str) -> bool:
        """Check if a thread is runnable (has pending messages after last stream.close)."""
        # A thread is runnable if there is a msg.create (user/tool/assistant with tool_calls)
        # strictly after the last stream.close.
        row_close = self.db.conn.execute(
            "SELECT MAX(event_seq) FROM events WHERE thread_id=? AND type='stream.close'",
            (tid,)
        ).fetchone()
        last_close_seq = int(row_close[0]) if row_close and row_close[0] is not None else -1
        row = self.db.conn.execute(
            """
            SELECT 1 FROM events e
             WHERE e.thread_id=?
               AND e.event_seq>?
               AND e.type='msg.create'
               AND (
                    json_extract(e.payload_json,'$.role') IN ('user','tool')
                 OR (
                      json_extract(e.payload_json,'$.role')='assistant'
                  AND json_extract(e.payload_json,'$.tool_calls') IS NOT NULL
                    )
               )
             LIMIT 1
            """,
            (tid, last_close_seq)
        ).fetchone()
        return bool(row)
