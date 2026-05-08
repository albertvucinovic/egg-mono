"""Tools-related command mixins for the egg application."""
from __future__ import annotations

import json
import os
from typing import List

from eggthreads import (
    approve_tool_calls_for_thread,
    append_message,
    create_snapshot,
)


class ToolCommandsMixin:
    """Mixin providing tool management commands."""

    def cmd_schedulers(self, arg: str) -> None:
        """Handle /schedulers command - list active schedulers."""
        if not self.active_schedulers:
            self.log_system('No active schedulers in this session.')
        else:
            out: List[str] = []
            for rid in self.active_schedulers.keys():
                out.append(f"- root {rid[-8:]}")
                out.append(self.format_tree(rid))
            block = "\n".join(out)
            self.log_system("Active SubtreeSchedulers (see console for full).")
            self.console_print_block('Schedulers', block, border_style='cyan')

    def enqueue_bash_tool(self, script: str, hidden: bool) -> None:
        """Enqueue a bash command as a user tool call (RA3).

        - For `$ cmd` (hidden=False): output is intended to be visible to the
          model, subject to output_approval gating.
        - For `$$ cmd` (hidden=True): we still execute the command and store
          the result in the thread, but mark the eventual tool message as
          no_api via the output_approval decision so the model does not see
          it.
        """
        cmd = (script or '').strip()
        if not cmd:
            self.log_system('Empty bash command, skipping.')
            return
        # Build a single tool_call entry for the bash tool
        tc_id = os.urandom(8).hex()
        tool_call = {
            'id': tc_id,
            'type': 'function',
            'function': {
                'name': 'bash',
                'arguments': json.dumps({'script': cmd}, ensure_ascii=False),
            },
        }
        extra = {
            'tool_calls': [tool_call],
            # Keep the user turn: the runner will execute the tool but we do
            # not immediately hand control to the LLM.
            'keep_user_turn': True,
            'user_command_type': '$$' if hidden else '$',
        }
        if hidden:
            # The user explicitly requested that this command output not be
            # shown to the model; we still allow the tool result to be stored
            # in the thread but mark this triggering message as no_api so it
            # is excluded from LLM context reconstruction.
            extra['no_api'] = True
        # Store the triggering user message (for transcript) and associated
        # tool_calls metadata. Visible commands use "$ ", hidden commands
        # use "$$ " so they are easy to distinguish in the transcript.
        prefix = '$$ ' if hidden else '$ '
        msg_id = append_message(self.db, self.current_thread, 'user', f"{prefix}{cmd}", extra=extra)
        # Automatically approve this tool call so it starts in TC2.1
        try:
            approve_tool_calls_for_thread(
                self.db,
                self.current_thread,
                decision='granted',
                reason='Approved as user-initiated command',
                tool_call_id=tc_id,
            )
        except Exception as e:
            self.log_system(f'Error approving tool call for bash command: {e}')
        # Snapshot and ensure scheduler so that RA3 will pick this up.
        try:
            create_snapshot(self.db, self.current_thread)
        except Exception:
            pass
        self.ensure_scheduler_for(self.current_thread)
        self.log_system(f"Queued bash command as tool_call {tc_id[-6:]} (hidden={hidden}).")
