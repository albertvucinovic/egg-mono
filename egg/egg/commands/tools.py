"""Tools-related command mixins for the egg application."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from eggthreads import approve_tool_calls_for_thread


class ToolCommandsMixin:
    """Mixin providing tool management commands."""

    def cmd_toggleAutoApproval(self, arg: str) -> None:
        """Handle /toggleAutoApproval command - toggle global tool auto-approval."""
        # Toggle per-thread global tool auto-approval by emitting a
        # tool_call.approval event with decision global_approval /
        # revoke_global_approval. This affects future tool calls in
        # this thread (both assistant- and user-originated).
        try:
            from eggthreads import build_tool_call_states  # type: ignore
            from eggthreads import thread_state as _thread_state  # type: ignore
        except Exception:
            self.log_system('Auto-approval toggle not available (eggthreads import failed).')
            return
        # Heuristic: check whether there exists any approval event with
        # decision == 'global_approval' more recent than any
        # revoke_global_approval; since we don't persist this flag
        # separately, we simply toggle based on the last such event.
        try:
            cur = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='tool_call.approval' ORDER BY event_seq ASC",
                (self.current_thread,),
            )
            last_decision = None
            for (pj,) in cur.fetchall():
                try:
                    payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
                except Exception:
                    payload = {}
                d = payload.get('decision')
                if d in ('global_approval', 'revoke_global_approval'):
                    last_decision = d
            enable = (last_decision != 'global_approval')
        except Exception:
            enable = True

        decision = 'global_approval' if enable else 'revoke_global_approval'
        try:
            approve_tool_calls_for_thread(
                self.db,
                self.current_thread,
                decision=decision,
                reason='Toggled by user via /toggleAutoApproval',
            )
            self.log_system(
                'Global tool auto-approval ENABLED for this thread.' if enable
                else 'Global tool auto-approval DISABLED for this thread.'
            )
        except Exception as e:
            self.log_system(f'Error toggling auto-approval: {e}')

    def cmd_toolsOn(self, arg: str) -> None:
        """Handle /toolsOn command - enable tools for thread."""
        # Thread-wide toggle: allow RA1 to expose tools again.
        try:
            from eggthreads import set_thread_tools_enabled  # type: ignore
            set_thread_tools_enabled(self.db, self.current_thread, True)
            self.log_system('Tools enabled for this thread (LLM may call tools).')
        except Exception as e:
            self.log_system(f'/toolson error: {e}')

    def cmd_toolsOff(self, arg: str) -> None:
        """Handle /toolsOff command - disable tools for thread."""
        # Thread-wide toggle: RA1 will not expose tools to the LLM
        # for this thread. User-initiated commands ($, $$, /wait)
        # still work as they are modelled as explicit tool calls.
        try:
            from eggthreads import set_thread_tools_enabled  # type: ignore
            set_thread_tools_enabled(self.db, self.current_thread, False)
            self.log_system('Tools disabled for this thread (LLM tool calls suppressed).')
        except Exception as e:
            self.log_system(f'/toolsoff error: {e}')

    def cmd_toolsSecrets(self, arg: str) -> None:
        """Handle /toolsSecrets command - toggle secrets masking."""
        # Toggle per-thread masking of potential secrets in tool
        # outputs. When masking is enabled (default), outputs from
        # tools such as bash/python are filtered to remove
        # problematic control characters and, when the optional
        # detect-secrets library is available, to mask values that
        # look like API keys or credentials. "on" = allow raw
        # output (no masking); "off" = mask secrets.
        mode = (arg or '').strip().lower()
        if mode not in ('on', 'off'):
            self.log_system('Usage: /toolsSecrets <on|off>  (on = allow raw tool output, off = mask secrets)')
            return
        allow_raw = (mode == 'on')
        try:
            from eggthreads import set_thread_allow_raw_tool_output  # type: ignore
            set_thread_allow_raw_tool_output(self.db, self.current_thread, allow_raw)
            if allow_raw:
                self.log_system('Tool output secrets: raw mode ENABLED (secrets will not be masked).')
            else:
                self.log_system('Tool output secrets: masking ENABLED (attempting to mask detected secrets).')
        except Exception as e:
            self.log_system(f'/toolsecrets error: {e}')

    def cmd_disableTool(self, arg: str) -> None:
        """Handle /disableTool command - disable a specific tool."""
        # Per-thread blacklist of individual tool names.
        name = (arg or '').strip()
        if not name:
            self.log_system('Usage: /disabletool <tool_name>')
            return
        try:
            from eggthreads import disable_tool_for_thread  # type: ignore
            disable_tool_for_thread(self.db, self.current_thread, name)
            self.log_system(f"Tool '{name}' disabled for this thread.")
        except Exception as e:
            self.log_system(f'/disabletool error: {e}')

    def cmd_enableTool(self, arg: str) -> None:
        """Handle /enableTool command - enable a specific tool."""
        name = (arg or '').strip()
        if not name:
            self.log_system('Usage: /enabletool <tool_name>')
            return
        try:
            from eggthreads import enable_tool_for_thread  # type: ignore
            enable_tool_for_thread(self.db, self.current_thread, name)
            self.log_system(f"Tool '{name}' enabled for this thread.")
        except Exception as e:
            self.log_system(f'/enabletool error: {e}')

    def cmd_toolsStatus(self, arg: str) -> None:
        """Handle /toolsStatus command - report tools configuration."""
        # Report effective tools configuration for the current
        # thread: whether LLM tools are enabled and which tools are
        # currently disabled.
        try:
            from eggthreads import get_thread_tools_config  # type: ignore
            cfg = get_thread_tools_config(self.db, self.current_thread)
        except Exception as e:
            self.log_system(f'/toolStatus error: {e}')
            return
        status = 'enabled' if cfg.llm_tools_enabled else 'disabled'
        disabled = sorted(cfg.disabled_tools) if cfg.disabled_tools else []
        secrets_mode = 'raw' if getattr(cfg, 'allow_raw_tool_output', False) else 'masked'
        lines = [
            f"Tools for this thread: {status}",
            "Disabled tools: " + (", ".join(disabled) if disabled else "(none)"),
            f"Tool output secrets mode: {secrets_mode}",
        ]
        self.log_system("\n".join(lines))

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
