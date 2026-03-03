"""Approval workflow mixin for the egg application."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from eggthreads import approve_tool_calls_for_thread, create_snapshot

from .utils import shorten_output_preview


class ApprovalMixin:
    """Mixin providing tool approval workflow methods for EggDisplayApp."""

    def compute_pending_prompt(self) -> None:
        """Compute whether there is an execution or output approval pending
        for the current thread and update self._pending_prompt accordingly.

        - Execution approval (TC1) is only for assistant tool calls.
        - Output approval (TC4) is for any tool call with finished_output
          present and no output_approval yet.

        To avoid spamming the System panel, we only log when the
        pending prompt *changes* (kind or ids).
        """
        try:
            from eggthreads import list_tool_calls_for_thread, thread_state
        except Exception:
            self._pending_prompt = {}
            return

        old = getattr(self, '_pending_prompt', {}) or {}
        new = {}
        try:
            st = thread_state(self.db, self.current_thread)
        except Exception:
            st = 'unknown'

        if st in ('waiting_tool_approval', 'waiting_output_approval'):
            try:
                tcs = list_tool_calls_for_thread(self.db, self.current_thread)
            except Exception:
                tcs = []
            # Prefer execution approval first
            exec_needed = [tc for tc in tcs if tc.state == 'TC1']
            if exec_needed:
                ids = [tc.tool_call_id for tc in exec_needed]
                new = {'kind': 'exec', 'tool_call_ids': ids}
            else:
                # Otherwise, check output approval for finished tool calls
                out_needed = [tc for tc in tcs if tc.state == 'TC4' and tc.finished_output]
                if out_needed:
                    ids = [tc.tool_call_id for tc in out_needed]
                    new = {'kind': 'output', 'tool_call_ids': ids}

        # Update and log only if changed
        if new != old:
            self._pending_prompt = new
            if not new:
                return
            if new.get('kind') == 'exec':
                self.log_system(
                    'Execution approval needed for some tool calls. '
                    'Type "y" to approve, "n" to deny, or "a" to approve all tool calls for this assistant turn.'
                )
            elif new.get('kind') == 'output':
                # Compose a size-aware prompt for the first pending long output.
                try:
                    from eggthreads import list_tool_calls_for_thread
                    tcs_all = list_tool_calls_for_thread(self.db, self.current_thread)
                except Exception:
                    tcs_all = []
                tc_for_msg = None
                ids = new.get('tool_call_ids') or []
                for tc in tcs_all:
                    if tc.tool_call_id in ids and tc.finished_output:
                        tc_for_msg = tc
                        break
                if tc_for_msg and isinstance(tc_for_msg.finished_output, str):
                    out = tc_for_msg.finished_output
                    line_count = len(out.splitlines())
                    char_count = len(out)
                    self.log_system(
                        f"This output is very long ({line_count} lines, {char_count} chars), "
                        "do you want to include all of it?([y]es/[n]o/[o]mit)"
                    )
                    preview = shorten_output_preview(out)
                    if preview:
                        self.log_system("Preview (shortened):\n" + preview)
                else:
                    # Fallback generic message if we cannot inspect the output.
                    self.log_system('Output approval needed for some tool calls. Type "y" to include, "n" to send a shortened preview, or "o" to omit.')

    def handle_pending_approval_answer(self, raw_text: str, source: str = 'Enter') -> bool:
        """Handle a pending tool approval/output-approval answer.

        raw_text: current input text (will be stripped/lowered)
        source: human-readable origin (e.g. 'Enter', 'Ctrl+D') for logging.

        Returns True if the key press was *fully handled* as part of the
        approval flow (whether the answer was valid or not). When this
        method returns True, the caller MUST NOT treat the text as a
        normal chat message.

        This behaviour is important for correctness of the tools
        protocol: if an assistant message with ``tool_calls`` is waiting
        for execution or output approval, we must not accidentally send
        arbitrary user messages to the model in between the assistant
        tool call and its corresponding tool messages. Doing so would
        violate the OpenAI tools protocol and cause hard provider
        errors.
        """
        try:
            pending = getattr(self, '_pending_prompt', {}) or {}
        except Exception:
            pending = {}
        # If there is no pending approval prompt, this key press is not
        # part of the approval flow and the caller may treat it as a
        # normal chat submission.
        if not pending:
            return False
        txt = (raw_text or '').strip().lower()
        # With a pending prompt, *any* non-empty input should be
        # interpreted as an attempt to answer that prompt, not as a
        # standalone chat message.  Empty input is ignored but still
        # considered handled so it does not accidentally create a blank
        # user message.
        if not txt:
            try:
                self.log_system('Approval pending: expected a response (e.g. y/n), empty input ignored.')
            except Exception:
                pass
            return True
        kind = pending.get('kind')
        ids = pending.get('tool_call_ids') or []
        try:
            from eggthreads import build_tool_call_states
        except Exception:
            ids = []
        # Exec approval: y = approve this set, n = deny this set,
        # a = approve all tool calls in this user turn (RA2 and RA3).
        if kind == 'exec' and ids and txt in ('y', 'n', 'a'):
            try:
                if txt in ('y', 'n'):
                    approve = (txt == 'y')
                    decision = 'granted' if approve else 'denied'
                    for tcid in ids:
                        approve_tool_calls_for_thread(
                            self.db,
                            self.current_thread,
                            decision=decision,
                            reason=f'Approved/denied by user from UI ({source})',
                            tool_call_id=tcid,
                        )
                    self.log_system(f"Tool calls {ids} approval decision: {decision}.")
                else:  # txt == 'a'
                    approve_tool_calls_for_thread(
                        self.db,
                        self.current_thread,
                        decision='all-in-turn',
                        reason=f'Approved by user from UI ({source})',
                    )
                    self.log_system(
                        f"Approved all tool calls for this user turn (decision=all-in-turn, via {source})."
                    )
            except Exception as e:
                self.log_system(f'Error recording approval: {e}')
            self._pending_prompt = {}
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        # Output approval for very long tool outputs:
        # y -> whole, n -> shortened preview, o -> omit.
        if kind == 'output' and ids and txt in ('y', 'n', 'o'):
            try:
                states = build_tool_call_states(self.db, self.current_thread)
                if txt == 'y':
                    decision = 'whole'
                elif txt == 'n':
                    decision = 'partial'
                else:
                    decision = 'omit'
                for tcid in ids:
                    tc = states.get(str(tcid))
                    if not tc or not tc.finished_output:
                        continue
                    full = tc.finished_output
                    if not isinstance(full, str):
                        full = str(full)
                    line_count = len(full.splitlines())
                    char_count = len(full)
                    if decision == 'whole':
                        preview = full
                    elif decision == 'partial':
                        preview = shorten_output_preview(full)
                    else:
                        preview = "Output omitted."
                    self.db.append_event(
                        event_id=os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tcid,
                            'decision': decision,
                            'reason': f'User decided in UI ({source})',
                            'preview': preview,
                            'line_count': line_count,
                            'char_count': char_count,
                        },
                    )
                self.log_system(f"Tool calls {ids} output decision: {decision} (via {source}).")
            except Exception as e:
                self.log_system(f'Error recording approval: {e}')
            self._pending_prompt = {}
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
            return True
        # We had a pending prompt but the answer was not one of the
        # recognised options. Treat this as an invalid approval answer,
        # not as a chat message. Keep the pending prompt so the user can
        # try again, but clear the input to avoid confusion.
        try:
            if kind == 'exec':
                self.log_system(
                    f"Unrecognised execution-approval answer {txt!r}; expected 'y', 'n', or 'a'. "
                    "The message was *not* sent to the model."
                )
            elif kind == 'output':
                self.log_system(
                    f"Unrecognised output-approval answer {txt!r}; expected 'y', 'n', or 'o'. "
                    "The message was *not* sent to the model."
                )
            else:
                self.log_system(
                    f"Unrecognised approval answer {txt!r}; the message was not sent as chat."
                )
        except Exception:
            pass
        try:
            self.input_panel.clear_text()
            self.input_panel.increment_message_count()
        except Exception:
            pass
        return True

    def cancel_pending_tools_on_interrupt(self) -> None:
        """Best-effort cancellation of pending or running tool calls.

        Used by Ctrl+C handling to stop any user command or tool execution
        from continuing without quitting the app.

        Semantics:
          - TC1 (needs approval): auto-deny execution.
          - TC2.1 / TC3 / TC4: mark output decision as "omit" so that any
            eventual results are not surfaced to the model and do not
            require further approval.
        """
        try:
            from eggthreads import build_tool_call_states
        except Exception:
            return
        try:
            states = build_tool_call_states(self.db, self.current_thread)
        except Exception:
            return
        if not states:
            return
        any_tool_msg = False
        for tcid, tc in states.items():
            try:
                # Skip tool calls that already have a published tool
                # message; they are protocol-complete.
                if getattr(tc, 'published', False):
                    continue

                tool_call_id = str(tcid)
                # TC1: needs approval -> deny execution entirely.
                if tc.state == 'TC1':
                    approve_tool_calls_for_thread(
                        self.db,
                        self.current_thread,
                        decision='denied',
                        reason='Cancelled by user via Ctrl+C',
                        tool_call_id=tool_call_id,
                    )
                    # For assistant-originated tool calls, also emit a
                    # synthetic tool response so that every assistant
                    # message with tool_calls has a corresponding tool
                    # message, even though execution never ran.
                    if getattr(tc, 'parent_role', None) == 'assistant':
                        name = getattr(tc, 'name', '') or tool_call_id
                        content = (
                            f"Tool call '{name}' was cancelled before it ran. "
                            "Reason: cancelled by user via Ctrl+C."
                        )
                        payload = {
                            'role': 'tool',
                            'content': content,
                            'tool_call_id': tool_call_id,
                            'name': name,
                        }
                        self.db.append_event(
                            event_id=os.urandom(10).hex(),
                            thread_id=self.current_thread,
                            type_='msg.create',
                            msg_id=os.urandom(10).hex(),
                            invoke_id=None,
                            payload=payload,
                        )
                        any_tool_msg = True
                # TC2.1 (approved), TC3 (executing), TC4 (finished, waiting
                # for output approval): mark output as omitted so the
                # runner will not auto-approve or surface it.
                elif tc.state in ('TC2.1', 'TC3', 'TC4'):
                    self.db.append_event(
                        event_id=os.urandom(10).hex(),
                        thread_id=self.current_thread,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tool_call_id,
                            'decision': 'omit',
                            'reason': 'Cancelled by user via Ctrl+C',
                            'preview': 'Output omitted (cancelled by user).',
                        },
                    )
                    # Assistant-originated tool calls should still get a
                    # tool message so that the tools protocol invariant
                    # holds and future LLM calls do not fail with
                    # missing responses for tool_call_ids.
                    if getattr(tc, 'parent_role', None) == 'assistant':
                        name = getattr(tc, 'name', '') or tool_call_id
                        content = (
                            f"Tool call '{name}' was interrupted and its output was omitted. "
                            "Reason: cancelled by user via Ctrl+C."
                        )
                        payload = {
                            'role': 'tool',
                            'content': content,
                            'tool_call_id': tool_call_id,
                            'name': name,
                        }
                        self.db.append_event(
                            event_id=os.urandom(10).hex(),
                            thread_id=self.current_thread,
                            type_='msg.create',
                            msg_id=os.urandom(10).hex(),
                            invoke_id=None,
                            payload=payload,
                        )
                        any_tool_msg = True
            except Exception:
                continue

        # If we synthesized any tool messages, rebuild the snapshot so
        # that subsequent LLM turns see a history that already contains
        # those tool responses. This avoids provider errors complaining
        # about missing tool messages for previously declared
        # tool_call_ids.
        if any_tool_msg:
            try:
                create_snapshot(self.db, self.current_thread)
            except Exception:
                pass
