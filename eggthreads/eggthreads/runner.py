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
from .tool_state import (
    ToolCallState,
    RunnerActionable,
    discover_runner_actionable,
    discover_runner_actionable_cached,
    thread_state,
    build_tool_call_states,
)
from .tools_config import get_thread_tools_config


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

        # Determine what kind of work (if any) is pending.  The cached
        # variant avoids repeatedly rebuilding tool-call state when the
        # event log has not changed for this thread.
        ra = discover_runner_actionable_cached(self.db, self.thread_id)
        if not ra:
            return False

        # Acquire lease with fresh invoke_id
        invoke_id = os.urandom(10).hex()
        lease_until = _now_plus(self.cfg.lease_ttl_sec)
        if not self.db.try_open_stream(self.thread_id, invoke_id, lease_until, owner=self.owner, purpose=self.purpose):
            return False

        # Resolve current model for this turn from eggthreads API so that
        # the provider call and the event annotations stay in sync. Fall
        # back to the LLM client's current_model_key if needed.
        current_model: Optional[str] = None
        try:
            from .api import current_thread_model
            current_model = current_thread_model(self.db, self.thread_id)
        except Exception:
            current_model = None
        if not current_model:
            try:
                current_model = getattr(self.llm, 'current_model_key', None)
            except Exception:
                current_model = None

        # For LLM turns, configure the underlying client before we start
        # streaming so that the model used for the provider call matches
        # the model we record in events.
        if ra.kind == 'RA1_llm' and current_model:
            try:
                self.llm.set_model(current_model)
            except Exception:
                pass

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
            # and ensure RA1 boundaries advance even if the provider fails
            # before any streaming deltas are emitted.
            try:
                # Emit a synthetic stream.delta with a 'reason' field so
                # _last_stream_close_seq() will treat this invoke_id as an
                # LLM stream. This prevents the same user message from
                # repeatedly triggering a failing RA1 turn.
                _append_delta({'reason': f'LLM/runner error: {e}', 'model_key': current_model})
            except Exception:
                pass
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

        # Build base_messages from snapshot, respecting no_api and
        # applying per-model thinking-content policy/options.
        th = self.db.get_thread(self.thread_id)
        base_messages: List[Dict[str, Any]] = []
        thinking_policy: Optional[str] = None
        thinking_key: Optional[str] = None
        # Resolve per-model options from the registry if possible.
        try:
            if self.llm is not None and current_model:
                from eggllm import LLMClient as _LLMClient  # type: ignore
                # We only access the registry via the concrete eggllm
                # client; other implementations may not expose it.
                if isinstance(self.llm, _LLMClient):
                    opts = self.llm.registry.model_options(current_model)  # type: ignore[attr-defined]
                    if isinstance(opts, dict):
                        tp = opts.get('thinking_content_policy')
                        if isinstance(tp, str) and tp.strip():
                            thinking_policy = tp.strip().lower()
                        tk = opts.get('thinking_content_key')
                        if isinstance(tk, str) and tk.strip():
                            thinking_key = tk.strip()
        except Exception:
            thinking_policy = None
            thinking_key = None

        if th and th.snapshot_json:
            try:
                snap = json.loads(th.snapshot_json)
                msgs = snap.get('messages', []) or []

                # If the model wants only the last assistant turn's
                # thinking, identify the index of the last user message
                # so we can treat messages after that as the "tail".
                last_user_idx = -1
                if thinking_policy == 'last assistant turn':
                    for i, m in enumerate(msgs):
                        try:
                            if m.get('role') == 'user' and isinstance(m.get('content'), str):
                                last_user_idx = i
                        except Exception:
                            continue

                def _maybe_include_reasoning(m: Dict[str, Any], idx: int) -> Optional[str]:
                    """Return reasoning text to send for this message, or None.

                    The snapshot uses ``reasoning`` to store thinking.
                    The provider may expect it under a different key,
                    configured via thinking_content_key.  We return the
                    thinking string here and let the caller attach it
                    under the appropriate key on the outbound message.
                    """
                    raw = m.get('reasoning') or m.get('reasoning_content')
                    if not isinstance(raw, str) or not raw:
                        return None
                    if thinking_policy == 'send all':
                        return raw
                    if thinking_policy == 'last assistant turn':
                        # Only include thinking for messages in the
                        # "tail" after the last user content.
                        if last_user_idx == -1 or idx <= last_user_idx:
                            return None
                        return raw
                    # Default / "strip all": never send.
                    return None

                for idx, m in enumerate(msgs):
                    if m.get('no_api'):
                        continue
                    r = m.get('role')
                    content = m.get('content', '')
                    # Compute optional thinking text according to policy
                    thinking_text = _maybe_include_reasoning(m, idx)
                    # Determine the outbound thinking key, defaulting
                    # to the provider's native "reasoning_content" if
                    # no explicit key was configured.
                    out_thinking_key = thinking_key or 'reasoning_content'

                    if r == 'assistant' and m.get('tool_calls'):
                        # Assistant messages with tool_calls may also
                        # carry thinking. We forward tool_calls plus any
                        # allowed thinking under the configured key.
                        msg_out: Dict[str, Any] = {
                            'role': 'assistant',
                            'tool_calls': m.get('tool_calls'),
                        }
                        if thinking_text is not None:
                            msg_out[out_thinking_key] = thinking_text
                        base_messages.append(msg_out)
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
                        msg_out: Dict[str, Any] = {'role': r, 'content': content}
                        if r == 'assistant' and thinking_text is not None:
                            msg_out[out_thinking_key] = thinking_text
                        base_messages.append(msg_out)
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

        # Track tool_call arguments as they stream so we can emit
        # incremental deltas into events for live UI rendering.  The
        # OpenAI-compatible adapter accumulates full ``arguments``
        # strings per tool_call; we compute the incremental tail per
        # invoke_id/tool_call_id and store only that tail in
        # stream.delta payloads.
        tool_calls_args_so_far: Dict[str, str] = {}

        # Final sanitation step before calling the provider: make sure that
        # user messages never carry "tool_calls" fields and that tool
        # exposure honours any per-thread tools configuration (e.g.
        # thread-wide tool disable, per-tool blacklists).
        base_messages = self._sanitize_messages_for_api(base_messages)

        # Apply per-thread tools configuration: this governs which tools
        # the LLM is allowed to see in this thread. User-initiated tools
        # (RA3) are still modelled as tool calls but are handled elsewhere
        # when executed.
        tools_cfg = get_thread_tools_config(self.db, self.thread_id)
        tools_spec = self.tools.tools_spec() or None
        if tools_spec is not None:
            # Filter out disabled tool names from the spec before
            # exposing them to the LLM.
            enabled_specs = []
            disabled_set = {n.lower() for n in tools_cfg.disabled_tools}
            for spec in tools_spec:
                try:
                    fn = (spec or {}).get('function') or {}
                    name = str(fn.get('name') or '').lower()
                    if name and name in disabled_set:
                        continue
                    enabled_specs.append(spec)
                except Exception:
                    enabled_specs.append(spec)
            tools_spec = enabled_specs or None

        # If thread-wide tools are disabled, suppress tools entirely for
        # this RA1 turn.
        if not tools_cfg.llm_tools_enabled:
            tools_spec_to_use = None
            tool_choice = None
        else:
            tools_spec_to_use = tools_spec
            tool_choice = 'auto'

        interrupted = False
        try:
            async for raw in self.llm.astream_chat(base_messages, tools=tools_spec_to_use, tool_choice=tool_choice):
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
                    elif et == 'tool_calls_delta':
                        # Stream tool_call arguments so that the live
                        # chat panel can display them as they arrive.
                        tcs = evt.get('delta') or []
                        if not isinstance(tcs, list):
                            tcs = []
                        for tc_delta in tcs:
                            if not isinstance(tc_delta, dict):
                                continue
                            tcid = str(tc_delta.get('id') or '')
                            fn = tc_delta.get('function') or {}
                            name = fn.get('name') or ''
                            args_full = fn.get('arguments') or ''
                            if not isinstance(args_full, str):
                                try:
                                    args_full = json.dumps(args_full, ensure_ascii=False)
                                except Exception:
                                    args_full = str(args_full)
                            prev = tool_calls_args_so_far.get(tcid, '')
                            if len(args_full) <= len(prev):
                                continue
                            delta_text = args_full[len(prev):]
                            if not delta_text:
                                continue
                            tool_calls_args_so_far[tcid] = args_full
                            # Heartbeat and stop if we lose the lease.
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
                                payload={
                                    'tool_call': {
                                        'id': tcid,
                                        'name': name,
                                        'arguments_delta': delta_text,
                                    },
                                    'model_key': current_model,
                                },
                            )
                            await asyncio.sleep(0)
                        if interrupted:
                            break
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
                        # If the provider returned an entirely empty
                        # assistant message (no content, no tools, no
                        # reasoning), skip creating a blank assistant
                        # msg and surface a system notice instead.
                        if not assistant_msg.get('content') and not assistant_msg.get('tool_calls') and not reasoning_parts:
                            err_payload: Dict[str, Any] = {
                                'role': 'system',
                                'content': 'LLM error: empty assistant message returned by provider',
                            }
                            if current_model:
                                err_payload['model_key'] = current_model
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='msg.create',
                                msg_id=os.urandom(10).hex(),
                                payload=err_payload,
                            )
                        else:
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
        # When reconstructing provider API messages, we must ensure tool
        # outputs do not leak secrets to the provider. We also sanitize
        # control characters to keep providers and downstream tooling
        # robust.
        try:
            tools_cfg = get_thread_tools_config(self.db, self.thread_id)
            allow_raw = bool(getattr(tools_cfg, 'allow_raw_tool_output', False))
        except Exception:
            allow_raw = False

        out: List[Dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            m2 = dict(m)
            role = m2.get("role")

            # RA3: user-command tool outputs -> plain user messages
            if role == "tool" and m2.get("user_tool_call") and not m2.get("no_api"):
                content = m2.get("content", "")
                # Mask secrets for provider API unless explicitly allowed.
                if isinstance(content, str) and not allow_raw:
                    try:
                        content = self._filter_tool_output(content, mask_secrets=True)
                    except Exception:
                        pass
                elif isinstance(content, str):
                    # Even in raw mode, still sanitize control chars.
                    try:
                        content = self._filter_tool_output(content, mask_secrets=False)
                    except Exception:
                        pass
                m2 = {"role": "user", "content": content}
                role = "user"

            # User messages must not carry tool_calls when sent to provider
            if role == "user" and "tool_calls" in m2:
                m2.pop("tool_calls", None)

            # For real tool outputs (role="tool" in the tools protocol),
            # mask secrets before sending to the provider unless raw mode
            # is explicitly enabled. This protects against accidental
            # leakage of credentials produced by tools.
            if role == "tool" and not m2.get("no_api"):
                content = m2.get("content")
                if isinstance(content, str):
                    if allow_raw:
                        try:
                            m2["content"] = self._filter_tool_output(content, mask_secrets=False)
                        except Exception:
                            pass
                    else:
                        try:
                            m2["content"] = self._filter_tool_output(content, mask_secrets=True)
                        except Exception:
                            pass

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
        # True iff we lost the lease and explicitly interrupted via
        # heartbeat failure; used to tag tool_call.finished.reason.
        interrupted_by_lease = False

        async def _hb_watcher():
            nonlocal interrupted_by_lease
            while True:
                await _asyncio.sleep(self.cfg.heartbeat_sec)
                # If bash has already completed naturally, stop watching.
                if proc.returncode is not None:
                    return
                # If we lose the lease (e.g. via interrupt_thread),
                # terminate the subprocess group.
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    interrupted_by_lease = True
                    try:
                        pgid = _os.getpgid(proc.pid)
                        _os.killpg(pgid, _signal.SIGTERM)
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    return

        # Concurrently read stdout/stderr so we can stream output as it
        # arrives. We accumulate into buffers while also emitting
        # stream.delta events for live display.
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        cancelled = False

        async def _stream_reader(stream, is_stdout: bool):
            nonlocal cancelled
            header_emitted = False
            prefix = '--- STDOUT ---\n' if is_stdout else '--- STDERR ---\n'
            while True:
                try:
                    chunk = await stream.readline()
                except Exception:
                    break
                if not chunk:
                    break
                # Decode, sanitize control characters, and apply cheap
                # per-line heuristic masking so that obvious secrets (like
                # .env keys) are not splashed into the UI during streaming.
                # We still perform the stronger masking pass when building
                # provider API messages.
                text_raw = chunk.decode(errors='replace')
                try:
                    text = self._filter_tool_output(text_raw, mask_secrets=True)
                except Exception:
                    text = text_raw
                if not header_emitted:
                    if is_stdout:
                        stdout_buf.append(prefix)
                    else:
                        stderr_buf.append(prefix)
                    header_emitted = True
                if is_stdout:
                    stdout_buf.append(text)
                else:
                    stderr_buf.append(text)

                # Stream this chunk immediately for live UI. If raw tool
                # output is not allowed (secrets masking enabled), do not
                # stream the actual content.
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    cancelled = True
                    break
                self.db.append_event(
                    event_id=_os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='stream.delta',
                    invoke_id=invoke_id,
                    chunk_seq=self.db.max_chunk_seq(invoke_id) + 1,
                    payload={'tool': {'name': tc.name or '', 'text': text, 'id': tc.tool_call_id}, 'model_key': current_model},
                )
                await _asyncio.sleep(0)

        watcher = _asyncio.create_task(_hb_watcher())
        stdout_task = _asyncio.create_task(_stream_reader(proc.stdout, True))
        stderr_task = _asyncio.create_task(_stream_reader(proc.stderr, False))

        try:
            await proc.wait()
            await _asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        finally:
            cancelled = True
            try:
                watcher.cancel()
                await _asyncio.sleep(0)
            except Exception:
                pass

        # Build combined output from accumulated buffers
        out = ''.join(stdout_buf) + ''.join(stderr_buf)
        full_result = out.strip() or "--- The command executed successfully and produced no output ---"

        # NOTE: We intentionally do not mask secrets here. Tool output may
        # contain secrets and we allow them to be stored and shown in the
        # local UI; secrets are only masked when building the provider API
        # request in _sanitize_messages_for_api(). We do keep the control-
        # character sanitization above for terminal safety.

        # Mark tool_call.finished with interrupted/success
        self.db.append_event(
            event_id=_os.urandom(10).hex(),
            thread_id=self.thread_id,
            type_='tool_call.finished',
            msg_id=None,
            invoke_id=invoke_id,
            payload={
                'tool_call_id': tc.tool_call_id,
                'reason': 'interrupted' if interrupted_by_lease else 'success',
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
                    event_id=_os.urandom(10).hex(),
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
        # Thread-level tools configuration (disables, etc.) is respected
        # both for assistant-originated tool calls (RA2) and
        # user-initiated ones (RA3).
        tools_cfg = get_thread_tools_config(self.db, self.thread_id)
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
                # Respect per-thread disabled tools: instead of
                # executing the tool, immediately mark it finished with
                # a synthetic "disabled" output. This applies equally
                # to assistant- and user-originated calls.
                if tc.name in tools_cfg.disabled_tools:
                    import os as _os
                    disabled_msg = (
                        f"Tool '{tc.name}' is disabled for this thread and "
                        "was not executed."
                    )
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='tool_call.finished',
                        msg_id=None,
                        invoke_id=invoke_id,
                        payload={
                            'tool_call_id': tc.tool_call_id,
                            'reason': 'disabled',
                            'output': disabled_msg,
                        },
                    )
                    # Immediately approve the small synthetic output so
                    # it can be published as a tool message on the next
                    # RA2/RA3 pass without user interaction.
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tc.tool_call_id,
                            'decision': 'whole',
                            'reason': 'Auto: tool disabled for this thread',
                            'preview': disabled_msg,
                        },
                    )
                    continue

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
                        # Pass thread_id and initial_model_key so tools
                        # like spawn_agent can infer their parent and
                        # inherit the model when not explicitly set.
                        lambda: self.tools.execute(
                            tc.name,
                            tc.arguments,
                            thread_id=self.thread_id,
                            initial_model_key=current_model,
                        ),
                    )
                except Exception as e:
                    full_result = f"ERROR: {e}"
                if not isinstance(full_result, str):
                    full_result = str(full_result)

                # We intentionally do not mask secrets in the stored tool
                # output or the live UI stream. Secrets are only prevented
                # from reaching the provider API in _sanitize_messages_for_api().
                # However, always sanitize control characters before
                # streaming to avoid terminal escape issues.
                try:
                    full_result = self._filter_tool_output(full_result, mask_secrets=False)
                except Exception:
                    pass
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
                finished_output = tc.finished_output or ''
                finished_reason = (tc.finished_reason or '').lower()

                # Determine base content. For interrupted tool calls we want
                # to surface the partial output that was produced before the
                # interruption so the user can inspect it (even if the
                # decision is "omit" for LLM context).
                if finished_reason == 'interrupted':
                    # Prefer an explicit preview when decision='partial';
                    # otherwise fall back to the full finished_output.
                    if decision == 'partial' and preview:
                        content = str(preview)
                    elif finished_output:
                        content = finished_output
                    else:
                        content = str(preview or "Output omitted.")
                    # Append a clear note so it is obvious this output is
                    # incomplete.
                    note = "Output incomplete - interrupted"
                    if content:
                        if not content.rstrip().endswith(note):
                            content = content.rstrip() + "\n\n" + note
                    else:
                        content = note
                else:
                    # Non-interrupted calls keep the previous semantics.
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


    def _mask_secrets_heuristic(self, text: str) -> str:
        """Fast, heuristic masking for secret-like substrings.

        This is intended for UI streaming or as a cheap first-pass before
        running heavier secret scanners.

        It is deliberately conservative (may over-mask).
        """
        import re as _re

        if not isinstance(text, str) or not text:
            return text

        # 1) .env-style assignments (KEY=..., TOKEN=..., etc.)
        # Preserve quotes when present.
        #
        # We also treat some *_ID variables as potentially sensitive, but
        # we do so conservatively to avoid masking harmless short ids.
        def _mask_env_line(m: "_re.Match[str]") -> str:
            lead = m.group(1) or ""
            name = (m.group(2) or "").strip()
            sep = m.group(3) or "="
            val = m.group(4) or ""
            val = val.strip()

            # If the name only matches because of "ID" (and not because it
            # contains other secret-like keywords), only mask when the value
            # looks high-entropy / secret-ish.
            if name and 'ID' in name:
                strong_keywords = (
                    'KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASS', 'PRIVATE', 'CREDENTIAL'
                )
                if not any(k in name for k in strong_keywords):
                    looks_secret = False
                    # long token-ish
                    if len(val) >= 24:
                        looks_secret = True
                    # UUID
                    if _re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", val.lower()):
                        looks_secret = True
                    # long hex
                    if _re.search(r"[A-Fa-f0-9]{16,}", val):
                        looks_secret = True
                    if not looks_secret:
                        return lead + name + sep + val

            if len(val) <= 1:
                return lead + name + sep + "***"
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                q = val[0]
                return lead + name + sep + q + "***" + q
            return lead + name + sep + "***"

        text = _re.sub(
            # Also match commented-out env lines, e.g.
            #   #API_KEY=...
            #   # export API_KEY=...
            #   #export API_KEY=...
            r"(?im)^(\s*(?:#\s*)?(?:export\s+)?)([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PRIVATE|CREDENTIAL|ID)[A-Z0-9_]*)(\s*=\s*)([^\r\n]+)$",
            _mask_env_line,
            text,
        )

        # 2) Authorization headers / bearer tokens
        text = _re.sub(r"(?i)(Authorization\s*:\s*Bearer\s+)([^\s\r\n]+)", r"\1***", text)
        text = _re.sub(r"(?i)(Bearer\s+)([^\s\r\n]+)", r"\1***", text)

        # 3) Common API token formats
        replacements = [
            (r"\bsk-[A-Za-z0-9]{20,}\b", "sk-***"),
            (r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b", "sk-ant-***"),
            (r"\bghp_[A-Za-z0-9]{20,}\b", "ghp_***"),
            (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "github_pat_***"),
            (r"\bhf_[A-Za-z0-9]{20,}\b", "hf_***"),
            (r"\bAKIA[0-9A-Z]{16}\b", "AKIA***"),
            (r"\bAIza[0-9A-Za-z\-_]{20,}\b", "AIza***"),
        ]
        for pat, rep in replacements:
            text = _re.sub(pat, rep, text)

        return text


    def _filter_tool_output(self, text: str, *, mask_secrets: bool = True) -> str:
        """Filter raw tool output before it is persisted or displayed.

        This performs two classes of filtering:

          1. Sanitize control characters that frequently confuse
             terminal emulators (e.g. stray escape sequences, other
             non-printables) while preserving newlines and
             tabs. Characters outside a conservative printable set are
             replaced with the Unicode replacement character.

          2. Best-effort masking of secret-like values using the
             optional ``detect-secrets`` library. When available, we
             run the built-in plugins against the output and mask the
             span of each detected secret with ``"***"``. When the
             library is not installed, we simply return the sanitized
             text as-is.

        Secret masking can be disabled (e.g. via a UI flag) by calling
        this helper with ``mask_secrets=False``. Control-character
        sanitization is always applied to protect the terminal.
        """

        if not isinstance(text, str) or not text:
            return text

        # 1) Strip/normalize problematic control characters but keep
        # standard whitespace intact.
        def _strip_control(s: str) -> str:
            out_chars: list[str] = []
            for ch in s:
                o = ord(ch)
                # Allow tab, newline, carriage return
                if ch in ('\t', '\n', '\r'):
                    out_chars.append(ch)
                # Printable ASCII and common extended range
                elif 32 <= o < 127:
                    out_chars.append(ch)
                elif 0xA0 <= o <= 0x10FFFF:
                    out_chars.append(ch)
                else:
                    # Replace other control bytes with a visible marker
                    out_chars.append('\uFFFD')
            return ''.join(out_chars)

        cleaned = _strip_control(text)

        # Cheap heuristic masking first.
        if mask_secrets:
            try:
                cleaned = self._mask_secrets_heuristic(cleaned)
            except Exception:
                pass

        # 2) Secret detection and masking (best-effort, optional dep).
        if not mask_secrets:
            return cleaned

        try:
            import importlib.util as _importlib_util

            if not _importlib_util.find_spec('detect_secrets'):
                return cleaned

            from detect_secrets import SecretsCollection  # type: ignore
            from detect_secrets.settings import default_settings  # type: ignore

            # We scan the output as a single in-memory "file".  The
            # detector API expects bytes and an associated filename.
            with default_settings():
                sc = SecretsCollection()
                # The scan method on SecretsCollection normally works
                # on files; for in-memory text we can use the
                # ``scan_lines`` helper available on individual
                # plugins. For portability across detect-secrets
                # versions, we instead call ``scan_file`` via a
                # temporary NamedTemporaryFile when necessary.

                import tempfile as _tempfile, os as _os

                with _tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                    tmp.write(cleaned)
                    tmp.flush()
                    tmp_path = tmp.name

                try:
                    sc.scan_file(tmp_path)
                finally:
                    try:
                        _os.unlink(tmp_path)
                    except Exception:
                        pass

                if not sc.data:
                    return cleaned

                # Mask all detected secrets by character span.  We
                # re-open the temp file contents to compute spans.
                # Since we already have ``cleaned`` in memory, we
                # simply operate on that string.
                secrets_for_file = next(iter(sc.data.values()), [])
                if not secrets_for_file:
                    return cleaned

                # Build a list of (start, end) index ranges to mask.
                # We intentionally avoid including trailing newline
                # characters in the span so we do not accidentally
                # join lines when masking.
                spans: list[tuple[int, int]] = []
                for sec in secrets_for_file:
                    try:
                        # Each ``sec`` has line_number and secret_hash
                        # but not the raw value; we conservatively mask
                        # the full line containing the secret.
                        line_no = int(getattr(sec, 'line_number', 0) or 0)
                    except Exception:
                        line_no = 0
                    if line_no <= 0:
                        continue
                    lines = cleaned.splitlines(keepends=True)
                    if 1 <= line_no <= len(lines):
                        line_txt = lines[line_no - 1]
                        start = sum(len(l) for l in lines[: line_no - 1])
                        # Exclude common line terminators from masking
                        # so the output formatting is preserved.
                        trim = line_txt.rstrip('\r\n')
                        end = start + len(trim)
                        if end > start:
                            spans.append((start, end))

                if not spans:
                    return cleaned

                # Merge overlapping spans and build masked string
                spans.sort()
                merged: list[tuple[int, int]] = []
                cur_start, cur_end = spans[0]
                for s, e in spans[1:]:
                    if s <= cur_end:
                        cur_end = max(cur_end, e)
                    else:
                        merged.append((cur_start, cur_end))
                        cur_start, cur_end = s, e
                merged.append((cur_start, cur_end))

                out_parts: list[str] = []
                last = 0
                mask = '***'
                for s, e in merged:
                    if last < s:
                        out_parts.append(cleaned[last:s])
                    out_parts.append(mask)
                    last = e
                if last < len(cleaned):
                    out_parts.append(cleaned[last:])
                return ''.join(out_parts)

        except Exception:
            # If anything goes wrong with detect-secrets integration,
            # fall back to the control-char-sanitised version.
            return cleaned

        return cleaned


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

        # Cheap per-thread event watermark to short-circuit expensive
        # runnable checks when a thread's event log has not changed
        # since the last iteration.
        last_checked_seq: Dict[str, int] = {}

        async def drive(tid: str):
            try:
                async with sem:
                    runner = ThreadRunner(
                        self.db,
                        tid,
                        llm=self.llm,
                        owner=self.owner,
                        purpose="assistant_stream",
                        config=self.cfg,
                        tools=self.tools,
                    )
                    try:
                        await runner.run_once()
                    except Exception:
                        # Swallow to keep loop alive; production code
                        # would log this event.
                        pass
            finally:
                # Remove from running set when done
                running_threads.discard(tid)

        from .api import is_thread_runnable

        while True:
            for tid in self._collect_subtree(self.root):
                if tid in running_threads:
                    continue

                # Quick cheap check: skip threads whose event log has
                # not changed since the last scheduler iteration.  This
                # avoids repeatedly running the relatively expensive
                # is_thread_runnable()/discover_runner_actionable logic
                # on completely idle threads.
                try:
                    max_seq = self.db.max_event_seq(tid)
                except Exception:
                    max_seq = -1
                if max_seq == last_checked_seq.get(tid, -1):
                    continue
                last_checked_seq[tid] = max_seq

                if is_thread_runnable(self.db, tid):
                    running_threads.add(tid)
                    asyncio.create_task(drive(tid))

            await asyncio.sleep(poll_sec)
