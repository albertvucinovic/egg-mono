"""Tools-related command mixins for the egg application."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from eggthreads import (
    approve_tool_calls_for_thread,
    append_message,
    create_snapshot,
)


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all available tools with their specs.

    Returns a dict mapping tool name to {"spec": ..., "local_only": bool}
    """
    from eggthreads.command_catalog import _get_available_tools

    return _get_available_tools()


class ToolCommandsMixin:
    """Mixin providing tool management commands."""

    def cmd_toggleAutoApproval(self, arg: str) -> None:
        """Handle /toggleAutoApproval command - toggle global tool auto-approval."""
        from eggthreads.command_catalog import _toggle_auto_approval_for_thread

        _toggle_auto_approval_for_thread(
            self.db,
            self.current_thread,
            self.log_system,
            approve_tool_calls_for_thread,
        )

    def cmd_toolsOn(self, arg: str) -> None:
        """Handle /toolsOn command - enable tools for thread."""
        from eggthreads.command_catalog import CommandContext, _tools_on_handler

        _tools_on_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def cmd_toolsOff(self, arg: str) -> None:
        """Handle /toolsOff command - disable tools for thread."""
        from eggthreads.command_catalog import CommandContext, _tools_off_handler

        _tools_off_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def cmd_toolsSecrets(self, arg: str) -> None:
        """Handle /toolsSecrets command - toggle secrets masking."""
        from eggthreads.command_catalog import CommandContext, _tools_secrets_handler

        _tools_secrets_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def cmd_disableTool(self, arg: str) -> None:
        """Handle /disableTool command - disable a specific tool."""
        from eggthreads.command_catalog import CommandContext, _disable_tool_handler

        _disable_tool_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def cmd_enableTool(self, arg: str) -> None:
        """Handle /enableTool command - enable a specific tool."""
        from eggthreads.command_catalog import CommandContext, _enable_tool_handler

        _enable_tool_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                app=self,
            ),
            arg,
        )

    def cmd_toolsStatus(self, arg: str) -> None:
        """Handle /toolsStatus command - report tools configuration and available tools."""
        from eggthreads.command_catalog import CommandContext, _tools_status_handler

        _tools_status_handler(
            CommandContext(
                db=self.db,
                current_thread=self.current_thread,
                log_system=self.log_system,
                console_print_block=self.console_print_block,
                app=self,
            ),
            arg,
        )

    def cmd_toolInfo(self, arg: str) -> None:
        """Handle /toolInfo command - show tool description in JSON format."""
        from eggthreads.command_catalog import CommandContext, _tool_info_handler

        _tool_info_handler(
            CommandContext(
                log_system=self.log_system,
                console_print_block=self.console_print_block,
                app=self,
            ),
            arg,
        )

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
