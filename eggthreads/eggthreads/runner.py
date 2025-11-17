from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from pathlib import Path
try:
    from eggllm import LLMClient
except Exception:
    LLMClient = None  # type: ignore
from .db import ThreadsDB
from .tools import ToolRegistry, create_default_tools
from .tool_state import ToolCallState, RunnerActionable, discover_runner_actionable, thread_state, build_tool_call_states


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
        if llm is not None:
            self.llm = llm
        elif LLMClient is not None:
            self.llm = LLMClient(models_path=models_path or 'models.json', all_models_path=all_models_path or 'all-models.json')
        else:
            self.llm = None
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
                        # Preserve user_tool_call so that RA3 user commands
                        # can be rewritten to user-role messages before
                        # hitting the provider API.
                        if m.get('user_tool_call'):
                            obj['user_tool_call'] = m.get('user_tool_call')
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
                    if payload.get('user_tool_call'):
                        obj['user_tool_call'] = payload.get('user_tool_call')
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

        # Final sanitation step before calling the provider: make sure that
        # user messages never carry "tool_calls" fields. User commands are
        # modelled as tool calls attached to user messages in the event log,
        # but when we send context to the API we want these to appear as
        # ordinary user messages (see ../egg/temp/approval-prompt.md).
        #
        # Most of this is already guaranteed by how we build base_messages
        # from the snapshot (we only copy role + content for user messages),
        # but we apply a dedicated sanitizer here so that any future changes
        # or external callers that add tool_calls to user messages cannot
        # accidentally leak them to the model.
        base_messages = self._sanitize_messages_for_api(base_messages)

        interrupted = False
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
                                interrupted = True
                                break
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
                                interrupted = True
                                break
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
                if interrupted:
                    break
        finally:
            # If the stream was interrupted (e.g. via Ctrl+C removing the
            # lease), we still want to persist whatever partial assistant
            # content we have as a user-visible message so that users can
            # inspect or edit it and the model can see what was interrupted.
            if interrupted and (assistant_text_parts or reasoning_parts):
                assistant_msg: Dict[str, Any] = {'role': 'assistant'}
                if assistant_text_parts:
                    assistant_msg['content'] = ''.join(assistant_text_parts)
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
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass


    def _sanitize_messages_for_api(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return a sanitized copy of messages for provider API.

        Responsibilities that belong specifically to eggthreads (and not
        to eggllm which is reused by other programs):

        - Convert RA3 user-command tool outputs (role="tool",
          user_tool_call=True) into plain user messages. The provider
          should never see these as tool-role messages; instead, they
          should look like "the user ran this command and saw this text".

        - Strip any ``tool_calls`` field from *user* messages so that
          user commands appear as ordinary user turns in the provider
          protocol.

        We intentionally **do not** touch assistant messages here,
        since their ``tool_calls`` and tool-role responses are the
        standard OpenAI-compatible tools protocol (RA2).
        """
        out: List[Dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            m2 = dict(m)
            role = m2.get("role")

            # RA3: user-command tool outputs -> plain user messages
            if role == "tool" and m2.get("user_tool_call") and not m2.get("no_api"):
                content = m2.get("content", "")
                m2 = {"role": "user", "content": content}
                role = "user"

            # User messages must not carry tool_calls when sent to provider
            if role == "user" and "tool_calls" in m2:
                m2.pop("tool_calls", None)

            # Some providers are strict about assistant/tool pairing and
            # will error if they see assistant messages with no content
            # between an assistant(tool_calls) and a tool message.  Blank
            # assistant messages carry no information, so we drop them
            # here to avoid confusing such templates.
            if role == "assistant":
                text = m2.get("content")
                if (text is None or (isinstance(text, str) and not text.strip())) \
                        and not m2.get("tool_calls"):
                    continue

            out.append(m2)
        return out


    async def _run_bash_tool_async(self, tc: ToolCallState, invoke_id: str, current_model: Optional[str], ra: RunnerActionable) -> None:
        """Execute a bash tool call with OS-level cancellation.

        This bypasses the generic ToolRegistry implementation so that we
        can terminate the underlying subprocess when the thread's lease
        is interrupted (e.g. via Ctrl+C in the UI).
        """
        import json as _json
        import asyncio as _asyncio
        import os as _os
        import signal as _signal

        # Decode arguments into a script string
        args = tc.arguments
        if isinstance(args, str):
            try:
                args_obj = _json.loads(args) if args.strip() else {}
            except Exception:
                args_obj = {"script": args}
        elif isinstance(args, dict):
            args_obj = args
        else:
            args_obj = {"script": str(args)}
        script = (args_obj.get("script") or "").strip()

        # Mark execution started
        self.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=self.thread_id,
            type_='tool_call.execution_started',
            msg_id=None,
            invoke_id=invoke_id,
            payload={'tool_call_id': tc.tool_call_id},
        )

        # Spawn bash subprocess in its own process group so we can kill
        # the entire command (sleep, child processes, etc.) on interrupt.
        proc = await _asyncio.create_subprocess_shell(
            script,
            executable='/bin/bash',
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            preexec_fn=_os.setsid,
        )
        cancelled = False

        async def _hb_watcher():
            nonlocal cancelled
            while True:
                await _asyncio.sleep(self.cfg.heartbeat_sec)
                if cancelled:
                    return
                # If we lose the lease (e.g. via interrupt_thread),
                # terminate the subprocess group.
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    cancelled = True
                    try:
                        pgid = _os.getpgid(proc.pid)
                        _os.killpg(pgid, _signal.SIGTERM)
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    return

        watcher = _asyncio.create_task(_hb_watcher())
        try:
            stdout, stderr = await proc.communicate()
        finally:
            cancelled = True
            try:
                watcher.cancel()
                await _asyncio.sleep(0)
            except Exception:
                pass

        # Build combined output (match default bash tool formatting)
        out = ''
        if stdout:
            out += f"--- STDOUT ---\n{stdout.decode().strip()}\n"
        if stderr:
            out += f"--- STDERR ---\n{stderr.decode().strip()}\n"
        full_result = out.strip() or "--- The command executed successfully and produced no output ---"

        # Stream the output as tool stream.deltas (respecting lease)
        CH = 400
        for i in range(0, len(full_result), CH):
            part = full_result[i : i + CH]
            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                cancelled = True
                break
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='stream.delta',
                invoke_id=invoke_id,
                chunk_seq=self.db.max_chunk_seq(invoke_id) + 1,
                payload={'tool': {'name': tc.name or '', 'text': part, 'id': tc.tool_call_id}, 'model_key': current_model},
            )
            await _asyncio.sleep(0)

        # Mark tool_call.finished with interrupted/success
        self.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=self.thread_id,
            type_='tool_call.finished',
            msg_id=None,
            invoke_id=invoke_id,
            payload={
                'tool_call_id': tc.tool_call_id,
                'reason': 'interrupted' if cancelled else 'success',
                'output': full_result,
            },
        )

        # Auto output-approval for small outputs, unless the UI (e.g.
        # Ctrl+C) already recorded an explicit decision for this
        # tool_call.
        try:
            lines = full_result.splitlines() if isinstance(full_result, str) else []
            line_count = len(lines)
            char_count = len(full_result) if isinstance(full_result, str) else 0
            is_long = line_count > 800 or char_count > 100000
            try:
                from .tool_state import build_tool_call_states
                states_now = build_tool_call_states(self.db, self.thread_id)
                existing = states_now.get(str(tc.tool_call_id))
                has_decision = bool(getattr(existing, 'output_decision', None))
            except Exception:
                has_decision = False
            if not is_long and not has_decision:
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.output_approval',
                    msg_id=None,
                    invoke_id=None,
                    payload={
                        'tool_call_id': tc.tool_call_id,
                        'decision': 'whole',
                        'reason': 'Auto: output below size thresholds',
                        'preview': full_result,
                    },
                )
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
                # Special-case bash so we can terminate the underlying
                # subprocess on Ctrl+C / lease loss.
                if tc.name == 'bash':
                    await self._run_bash_tool_async(tc, invoke_id, current_model, ra)
                    continue

                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.execution_started',
                    msg_id=None,
                    invoke_id=invoke_id,
                    payload={'tool_call_id': tc.tool_call_id},
                )
                # Run tools in a background thread to avoid blocking the
                # asyncio event loop, which is especially important for
                # tools like `wait` that may sleep and poll.
                loop = asyncio.get_running_loop()
                try:
                    full_result = await loop.run_in_executor(
                        None,
                        lambda: self.tools.execute(tc.name, tc.arguments),
                    )
                except Exception as e:
                    full_result = f"ERROR: {e}"
                if not isinstance(full_result, str):
                    full_result = str(full_result)
                out = full_result or ''
                CH = 400
                cancelled = False
                for i in range(0, len(out), CH):
                    part = out[i : i + CH]
                    # Respect the per-thread lease: if we lose it (e.g. via
                    # interrupt_thread on Ctrl+C), stop streaming further
                    # tool output for this invoke.
                    if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                        cancelled = True
                        break
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
                    payload={
                        'tool_call_id': tc.tool_call_id,
                        'reason': 'interrupted' if cancelled else 'success',
                        'output': full_result,
                    },
                )
                # Auto output-approval for small outputs (chat.sh style):
                # - if output is not excessively long in lines or characters,
                #   mark it as decision="whole" so the UI does not need to
                #   prompt the user and the runner can publish it on the next
                #   pass. Large outputs will remain in TC4 and require an
                #   explicit tool_call.output_approval from the UI.
                try:
                    lines = full_result.splitlines() if isinstance(full_result, str) else []
                    line_count = len(lines)
                    char_count = len(full_result) if isinstance(full_result, str) else 0
                    is_long = line_count > 800 or char_count > 100000
                    # If a user/output decision already exists for this tool
                    # call (e.g. Ctrl+C marked it as "omit"), do not
                    # override it with an automatic "whole" approval.
                    try:
                        from .tool_state import build_tool_call_states
                        states_now = build_tool_call_states(self.db, self.thread_id)
                        existing = states_now.get(str(tc.tool_call_id))
                        has_decision = bool(getattr(existing, 'output_decision', None))
                    except Exception:
                        has_decision = False
                    if not is_long and not has_decision:
                        self.db.append_event(
                            event_id=os.urandom(10).hex(),
                            thread_id=self.thread_id,
                            type_='tool_call.output_approval',
                            msg_id=None,
                            invoke_id=None,
                            payload={
                                'tool_call_id': tc.tool_call_id,
                                'decision': 'whole',
                                'reason': 'Auto: output below size thresholds',
                                'preview': full_result,
                            },
                        )
                except Exception:
                    # Best-effort only; on any error the call will remain
                    # in TC4 and the UI can still request explicit output
                    # approval from the user.
                    pass

            # Output approval done (TC5) -> publish final tool message based on
            # the last tool_call.output_approval payload.
            if tc.state == 'TC5':
                payload = tc.last_output_approval_payload or {}
                decision = payload.get('decision')
                preview = payload.get('preview') or ''
                # Determine base content from the approved preview.
                if decision == 'omit':
                    # User chose to omit the (possibly huge) output; we keep a
                    # small placeholder string instead of the real content.
                    content = "Output omitted."
                else:
                    # 'whole' or 'partial' (or unknown) -> use the preview string.
                    content = str(preview)

                # For user-originated commands ($ / $$), prepend the original
                # command text so that the message containing the output also
                # includes the command itself.
                if ra.kind == 'RA3_tools_user':
                    cmd_text = self._get_parent_message_content(tc.parent_msg_id)
                    if not cmd_text:
                        cmd_text = self._render_tool_invocation(tc)
                    if cmd_text:
                        # Avoid duplicating the command if the preview already starts with it.
                        if not content.startswith(cmd_text):
                            content = f"{cmd_text}\n\n{content}" if content else cmd_text

                # no_api rules:
                #  - For user-initiated commands (RA3), the model should not
                #    see this tool message at all when either the decision is
                #    "omit" *or* the parent user message was marked no_api
                #    (hidden "$$" commands). Visible "$" commands only hide
                #    the output when the decision is "omit".
                parent_no_api = self._parent_msg_has_no_api(tc.parent_msg_id) if ra.kind == 'RA3_tools_user' else False
                no_api_flag = bool(ra.kind == 'RA3_tools_user' and (decision == 'omit' or parent_no_api))

                msg = {
                    'role': 'tool',
                    'content': content,
                    'tool_call_id': tc.tool_call_id,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                # For user-initiated commands (RA3), keep the user turn
                # after publishing the tool result. The model should not
                # be invoked automatically; instead, the result becomes
                # part of the context for the *next* user message.
                if ra.kind == 'RA3_tools_user':
                    msg['keep_user_turn'] = True
                if no_api_flag:
                    msg['no_api'] = True
                if current_model:
                    msg['model_key'] = current_model
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=msg,
                )

    def _get_parent_message_content(self, msg_id: str) -> Optional[str]:
        """Best-effort lookup of the original message content for a tool call.

        This is used primarily for user-initiated commands so that the
        final message containing the tool output also includes the
        original command text for readability.
        """
        if not msg_id:
            return None
        try:
            cur = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE msg_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
                (msg_id,),
            )
            row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            payload = {}
        content = payload.get('content')
        return content if isinstance(content, str) else None

    def _parent_msg_has_no_api(self, msg_id: str) -> bool:
        """Check whether the parent message for a tool call was tagged no_api.

        This is used to propagate the hidden semantics of "$$" user
        commands to their eventual tool result messages so that the
        provider never sees them.
        """
        if not msg_id:
            return False
        try:
            cur = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE msg_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
                (msg_id,),
            )
            row = cur.fetchone()
        except Exception:
            return False
        if not row:
            return False
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            payload = {}
        return bool(payload.get('no_api'))

    def _render_tool_invocation(self, tc: ToolCallState) -> str:
        """Render a human-readable representation of a tool invocation.

        Used as a fallback when we cannot recover the user's original
        command text (e.g. if the parent message was edited or missing).
        """
        try:
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args_obj = json.loads(args) if args.strip() else {}
                except Exception:
                    args_obj = {"_raw": args}
            elif isinstance(args, dict):
                args_obj = args
            else:
                args_obj = {"_arg": args}
            if tc.name in ("bash", "python") and isinstance(args_obj.get("script"), str):
                script = args_obj.get("script")
                if tc.name == "bash":
                    return f"$ {script}"
                return f"python {script}"
            # Generic fallback
            return f"{tc.name}({json.dumps(args_obj, ensure_ascii=False)})"
        except Exception:
            return ""


class SubtreeScheduler:
    """Async orchestrator: watches a root thread and runs runnable threads within its subtree, up to concurrency limit."""

    def __init__(self, db: ThreadsDB, root_thread_id: str, llm: Optional[LLMClient] = None, owner: Optional[str] = None, config: Optional[RunnerConfig] = None,
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None):
        self.db = db
        self.root = root_thread_id
        if llm is not None:
            self.llm = llm
        elif LLMClient is not None:
            self.llm = LLMClient(models_path=models_path or 'models.json', all_models_path=all_models_path or 'all-models.json')
        else:
            self.llm = None
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
