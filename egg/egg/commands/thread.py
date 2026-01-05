"""Thread management command mixins for the egg application."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from eggthreads import (
    append_message,
    approve_tool_calls_for_thread,
    create_root_thread,
    create_snapshot,
    delete_thread,
    get_parent,
    list_threads,
)

from utils import MODELS_PATH, get_subtree as _get_subtree


class ThreadCommandsMixin:
    """Mixin providing thread management commands."""

    def cmd_newThread(self, arg: str) -> None:
        """Handle /newThread command - create a new root thread."""
        new_name = (arg or '').strip() or 'Root'
        cur_model_key = self.current_model_for_thread(self.current_thread) or None
        new_root = create_root_thread(self.db, name=new_name, initial_model_key=cur_model_key, models_path=str(MODELS_PATH))
        append_message(self.db, new_root, 'system', self.system_prompt)
        create_snapshot(self.db, new_root)
        self.ensure_scheduler_for(new_root)
        self.current_thread = new_root
        asyncio.get_running_loop().create_task(self.start_watching_current())
        self.log_system(f"Created new root thread: {new_root[-8:]}")
        self.print_static_view_current(heading=f"Switched to thread: {self.current_thread}")

    def cmd_spawnChildThread(self, arg: str, text: str = '') -> None:
        """Handle /spawnChildThread command - spawn a child thread."""
        # Use the spawn_agent tool implementation from eggthreads so we
        # share the same semantics between UI (/spawn) and model tools.
        def _latest_model_for_thread(tid: str) -> Optional[str]:
            # Mirror current_model_for_thread so that spawned
            # children inherit the same effective model as their
            # parent thread.
            try:
                from eggthreads import current_thread_model  # type: ignore
                return current_thread_model(self.db, tid)
            except Exception:
                th = self.db.get_thread(tid)
                imk = th.initial_model_key if th and isinstance(th.initial_model_key, str) else None
                return imk.strip() if imk and imk.strip() else None

        cur_model = _latest_model_for_thread(self.current_thread)

        # Import ToolRegistry/create_default_tools in a local scope to
        # avoid circular imports at module import time.
        try:
            from eggthreads.tools import create_default_tools  # type: ignore
            tools = create_default_tools()
            # Ensure spawn_agent exists; if not, this will raise.
            args = {
                # Parent is this UI thread; we pass it explicitly so
                # spawn_agent behaves the same as model-initiated calls
                # that receive thread_id via the runner context.
                'parent_thread_id': self.current_thread,
                'context_text': arg or 'Spawned task',
                'label': 'spawn',
                'system_prompt': self.system_prompt,
            }
            if cur_model:
                args['initial_model_key'] = cur_model
            # When called directly from the UI, we do not rely on the
            # implicit _thread_id injection and pass parent id
            # explicitly.
            res = tools.execute('spawn_agent', args)
        except Exception as e:
            self.log_system(f"/spawn error: {e}")
            return

        if not isinstance(res, str):
            self.log_system(f"/spawn returned non-string thread id: {res!r}")
            return

        child = res

        # Child thread now exists and has the system prompt + user
        # message and optional model marker already seeded by the
        # tool. We only need to ensure a scheduler is running and
        # log the result.
        self.ensure_scheduler_for(child)
        self.log_system(f"Spawned thread: {child[-8:]}")

        # Also append a user-visible command output message so the
        # spawned thread id becomes part of the conversation
        # context, similar to other user commands.
        try:
            cmd_text = text.strip() if text else f"/spawnChildThread {arg}".strip()
            msg_content = f"Command: {cmd_text}\n\nOutput:\n{child}"
            append_message(self.db, self.current_thread, 'user', msg_content, extra={'keep_user_turn': True})
            create_snapshot(self.db, self.current_thread)
        except Exception:
            pass

    def cmd_spawnAutoApprovedChildThread(self, arg: str) -> None:
        """Handle /spawnAutoApprovedChildThread command - spawn with auto-approval."""
        # Same as /spawn, but use spawn_agent_auto so the spawned
        # child has global tool auto-approval.
        def _latest_model_for_thread(tid: str) -> Optional[str]:
            # Mirror current_model_for_thread so that spawned
            # children inherit the same effective model as their
            # parent thread.
            try:
                from eggthreads import current_thread_model  # type: ignore
                return current_thread_model(self.db, tid)
            except Exception:
                th = self.db.get_thread(tid)
                imk = th.initial_model_key if th and isinstance(th.initial_model_key, str) else None
                return imk.strip() if imk and imk.strip() else None

        cur_model = _latest_model_for_thread(self.current_thread)

        try:
            from eggthreads.tools import create_default_tools  # type: ignore
            tools = create_default_tools()
            args = {
                'parent_thread_id': self.current_thread,
                'context_text': arg or 'Spawned task',
                'label': 'spawn_auto',
                'system_prompt': self.system_prompt,
            }
            if cur_model:
                args['initial_model_key'] = cur_model
            res = tools.execute('spawn_agent_auto', args)
        except Exception as e:
            self.log_system(f"/spawn_auto error: {e}")
            return

        if not isinstance(res, str):
            self.log_system(f"/spawn_auto returned non-string thread id: {res!r}")
            return

        child = res
        self.ensure_scheduler_for(child)
        self.log_system(f"Spawned auto-approval thread: {child[-8:]}")

    def cmd_waitForThreads(self, arg: str) -> None:
        """Handle /waitForThreads command - wait for child threads to complete."""
        # Treat /wait as a user command that enqueues a wait tool
        # call (RA3). The argument is a space-separated list of
        # thread selectors; use the same resolution logic as /thread
        # (via resolve_single_thread_selector) for maximum DRYness.

        arg_txt = (arg or '').strip()
        if not arg_txt:
            self.log_system('Usage: /wait <thread-id|suffix|name|recap-fragment>[,more...]')
            return

        # Support comma- or whitespace-separated selectors, e.g.
        #   /wait abc,def ghi
        # becomes selectors ['abc', 'def', 'ghi'].
        selectors = [s for s in re.split(r'[\s,]+', arg_txt) if s]
        resolved: list[str] = []
        for sel in selectors:
            tid = self.resolve_single_thread_selector(sel)
            if not tid:
                self.log_system(f"/wait: no thread matches selector '{sel}'")
                return
            resolved.append(tid)

        # Enqueue a wait tool call via the RA3 mechanism. We do not
        # hide it from the model by default; the model should see the
        # summary of the waited threads.
        tc_id = os.urandom(8).hex()
        tool_call = {
            'id': tc_id,
            'type': 'function',
            'function': {
                'name': 'wait',
                'arguments': json.dumps({'thread_ids': resolved}, ensure_ascii=False),
            },
        }
        extra = {
            'tool_calls': [tool_call],
            'keep_user_turn': True,
            'user_command_type': '/wait',
        }
        # Store the triggering user message and associated tool_calls
        msg_id = append_message(self.db, self.current_thread, 'user', f"/wait {arg_txt}", extra=extra)
        # Auto-approve this user-initiated tool call
        try:
            approve_tool_calls_for_thread(
                self.db,
                self.current_thread,
                decision='granted',
                reason='Approved as user-initiated /wait command',
                tool_call_id=tc_id,
            )
        except Exception as e:
            self.log_system(f'Error approving tool call for wait command: {e}')
        try:
            create_snapshot(self.db, self.current_thread)
        except Exception:
            pass
        self.ensure_scheduler_for(self.current_thread)
        self.log_system(f"Queued /wait for threads: {' '.join([tid[-8:] for tid in resolved])}.")

    def cmd_listChildren(self, arg: str) -> None:
        """Handle /listChildren command - list child threads."""
        sub = _get_subtree(self.db, self.current_thread)
        if not sub:
            self.log_system('No subthreads.')
        else:
            block = self.format_tree(self.current_thread)
            self.log_system('Subtree (see console for full):')
            self.console_print_block('Subtree', block, border_style='blue')

    def cmd_parentThread(self, arg: str) -> None:
        """Handle /parentThread command - move to parent thread."""
        pid = get_parent(self.db, self.current_thread)
        if pid:
            self.current_thread = pid
            asyncio.get_running_loop().create_task(self.start_watching_current())
            self.log_system('Moved to parent thread')
            self.print_static_view_current(heading=f"Switched to thread: {self.current_thread}")
        else:
            self.log_system('Already at root or no parent found.')

    def cmd_threads(self, arg: str) -> None:
        """Handle /threads command - list all threads."""
        try:
            text = self.format_tree()
            self.log_system('Threads by subtree (see console for full).')
            self.console_print_block('Threads', text, border_style='blue')
        except Exception as e:
            self.log_system(f"Error listing threads: {e}")

    def cmd_thread(self, arg: str) -> None:
        """Handle /thread command - switch to a thread."""
        sel = (arg or '').strip()
        if not sel:
            self.log_system(f"Current thread: {self.current_thread}")
        else:
            matches = self.select_threads_by_selector(sel)
            if not matches and ' ' in sel:
                sel_first = sel.split()[0]
                matches = self.select_threads_by_selector(sel_first)
            if not matches:
                try:
                    rows_all = list_threads(self.db)
                    suf = sel.lower()
                    matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
                except Exception:
                    matches = []
            if not matches:
                self.log_system(f"No thread matches selector: {sel}")
            else:
                try:
                    rows = list_threads(self.db)
                    ca = {r.thread_id: r.created_at for r in rows}
                except Exception:
                    ca = {}
                matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
                new_tid = matches[0]
                self.ensure_scheduler_for(new_tid)
                self.current_thread = new_tid
                asyncio.get_running_loop().create_task(self.start_watching_current())
                self.log_system(f"Switched to thread: {new_tid[-8:]}")
                self.print_static_view_current(heading=f"Switched to thread: {self.current_thread}")

    def cmd_deleteThread(self, arg: str) -> None:
        """Handle /deleteThread command - delete a thread."""
        selector = (arg or '').strip()
        if not selector:
            self.log_system('Usage: /delete <thread-id|suffix|name|recap-fragment>')
            return
        matches = self.select_threads_by_selector(selector)
        if not matches and ' ' in selector:
            sel_first = selector.split()[0]
            matches = self.select_threads_by_selector(sel_first)
        if not matches:
            try:
                rows_all = list_threads(self.db)
                suf = selector.lower()
                matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
            except Exception:
                matches = []
        matches = [m for m in matches if m != self.current_thread]
        if not matches:
            self.log_system('No deletable thread matches selector.')
            return
        try:
            rows = list_threads(self.db)
            ca = {r.thread_id: r.created_at for r in rows}
        except Exception:
            ca = {}
        matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
        target_tid = matches[0]
        try:
            delete_thread(self.db, target_tid)
            self.log_system(f"Thread {target_tid[-8:]} deleted.")
        except Exception as e:
            self.log_system(f'Error deleting thread: {e}')

    def cmd_duplicateThread(self, arg: str) -> None:
        """Handle /duplicateThread command - duplicate current thread."""
        # Duplicate the current thread as a new root thread, acting
        # as a "checkpoint" copy of the entire conversation up to
        # this point. The new thread has the same history (events
        # and snapshot) but no open stream and no parent/children
        # links. This is useful for branching or backups.
        try:
            from eggthreads import duplicate_thread  # type: ignore
        except Exception as e:
            self.log_system(f'/duplicate_thread not available: eggthreads import failed: {e}')
            return
        label = (arg or '').strip() or None
        try:
            new_tid = duplicate_thread(self.db, self.current_thread, name=label)
        except Exception as e:
            self.log_system(f'/duplicateThread error: {e}')
            return
        # Ensure a scheduler is running for the duplicate so it can
        # be continued independently if desired.
        self.ensure_scheduler_for(new_tid)
        self.log_system(f"Duplicated current thread to new root: {new_tid[-8:]}")
        # Switch to the duplicate so the user can inspect/continue it.
        self.current_thread = new_tid
        asyncio.get_running_loop().create_task(self.start_watching_current())
        self.print_static_view_current(heading=f"Switched to duplicated thread: {self.current_thread}")

    # ---- Thread selector helpers ----
    def select_threads_by_selector(self, selector: str) -> List[str]:
        """Select threads matching a selector (id, suffix, name, or recap fragment)."""
        try:
            rows = list_threads(self.db)
        except Exception:
            rows = []
        sel_l = (selector or '').lower()
        matches: List[str] = []
        for r in rows:
            if r.thread_id == selector:
                matches = [r.thread_id]
                break
        if not matches and sel_l:
            suf = [r.thread_id for r in rows if r.thread_id.lower().endswith(sel_l)]
            if suf:
                matches = suf
        if not matches and sel_l:
            cont = [r.thread_id for r in rows if sel_l in r.thread_id.lower()]
            if cont:
                matches = cont
        if not matches and sel_l:
            name_matches = [r.thread_id for r in rows if isinstance(r.name, str) and sel_l in r.name.lower()]
            if name_matches:
                matches = name_matches
        if not matches and sel_l:
            recap_matches = [r.thread_id for r in rows if isinstance(r.short_recap, str) and sel_l in r.short_recap.lower()]
            if recap_matches:
                matches = recap_matches
        return matches

    def resolve_single_thread_selector(self, selector: str) -> Optional[str]:
        """Resolve a free-form thread selector to a single thread_id.

        This wraps select_threads_by_selector with the same additional
        fallbacks and created_at ordering used by /thread and /delete so
        that other commands (e.g. /wait) can reuse the exact selector
        semantics.
        """
        sel = (selector or '').strip()
        if not sel:
            return None

        matches = self.select_threads_by_selector(sel)
        if not matches and ' ' in sel:
            sel_first = sel.split()[0]
            matches = self.select_threads_by_selector(sel_first)
        if not matches:
            try:
                rows_all = list_threads(self.db)
                suf = sel.lower()
                matches = [r.thread_id for r in rows_all if r.thread_id.lower().endswith(suf)]
            except Exception:
                matches = []
        if not matches:
            return None

        # Order by created_at newest-first, mirroring /thread behavior
        try:
            rows = list_threads(self.db)
            ca = {r.thread_id: r.created_at for r in rows}
        except Exception:
            ca = {}
        matches.sort(key=lambda tid: ca.get(tid, ''), reverse=True)
        return matches[0]

    # ---- Thread hierarchy helpers ----
    def thread_root_id(self, tid: str) -> str:
        """Return the root thread id for any thread id.

        Egg's SubtreeScheduler is keyed by *root* thread id. The UI
        needs a reliable way to map any thread in a subtree to its root
        so we can accurately mark threads as "scheduled" in the tree.

        We primarily use the backend's get_parent() helper (shared
        semantics with eggthreads). We also keep a tiny SQL fallback in
        case get_parent is unavailable or fails.
        """
        from typing import Optional

        cur = tid
        seen: set[str] = set()
        # Hard cap to avoid infinite loops in case of corrupted parent
        # links.
        for _ in range(2048):
            if not cur:
                break
            if cur in seen:
                # Cycle detected; best-effort: treat the current node as
                # the root to avoid crashing the UI.
                return cur
            seen.add(cur)

            parent: Optional[str] = None
            try:
                parent = get_parent(self.db, cur)
            except Exception:
                parent = None
            if parent is None:
                # Fallback (should be equivalent to get_parent)
                try:
                    row = self.db.conn.execute(
                        'SELECT parent_id FROM children WHERE child_id=?',
                        (cur,),
                    ).fetchone()
                    parent = row[0] if row and row[0] else None
                except Exception:
                    parent = None

            if not parent:
                return cur
            cur = parent

        return cur or tid

    def is_thread_scheduled(self, tid: str) -> bool:
        """True if tid's root has an entry in active_schedulers."""
        rid = self.thread_root_id(tid)
        return rid in (self.active_schedulers or {})
