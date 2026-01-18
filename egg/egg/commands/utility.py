"""Utility command mixins for the egg application."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from utils import COMMANDS_TEXT, read_clipboard
from eggthreads import set_context_limit, get_context_limit


class UtilityCommandsMixin:
    """Mixin providing utility commands: /help, /cost, /paste, /quit, /enterMode."""

    def cmd_help(self, arg: str) -> None:
        """Handle /help command - show available commands."""
        # Mirror /threads behaviour: show the full help text in the
        # console (above the live panels) and keep the System panel
        # message short.
        try:
            self.log_system('Help (see console for full).')
            self.console_print_block('Help', COMMANDS_TEXT.strip(), border_style='blue')
        except Exception:
            # Fallback: at least log it.
            self.log_system(COMMANDS_TEXT)

    def cmd_paste(self, arg: str) -> None:
        """Handle /paste command - paste clipboard content to input."""
        content = read_clipboard()
        if content is None:
            self.log_system('Failed to read clipboard.')
        elif content == '':
            self.log_system('Clipboard is empty.')
        else:
            self.input_panel.editor.editor.set_text(content)
            # Move cursor to start of pasted text so user sees beginning
            self.input_panel.editor.editor.cursor.row = 0
            self.input_panel.editor.editor.cursor.col = 0
            self.input_panel.editor.editor._clamp_cursor()
            # Reset scroll positions to show from start
            self.input_panel._scroll_top = 0
            self.input_panel._hscroll_left = 0
            self.log_system(f'Pasted {len(content)} characters from clipboard.')

    def cmd_quit(self, arg: str) -> None:
        """Handle /quit command - exit the application."""
        self.running = False

    def cmd_enterMode(self, arg: str) -> None:
        """Handle /enterMode command - set enter key behavior."""
        mode = (arg or '').strip().lower()
        if mode in ('send', 's', 'on'):
            self.enter_sends = True
            self.log_system('Enter mode: send (Enter sends, Ctrl+D also sends).')
        elif mode in ('newline', 'n', 'off'):
            self.enter_sends = False
            self.log_system('Enter mode: newline (Enter inserts newline, Ctrl+D sends).')
        else:
            self.log_system('Usage: /enterMode <send|newline>')

    def cmd_cost(self, arg: str) -> None:
        """Handle /cost command - show token usage and cost."""
        # Show token usage and approximate cost for the current thread.
        # Reuse current_token_stats so that /cost and the Chat
        # Messages title always agree on the underlying numbers.
        ctx_tokens, api = self.current_token_stats()
        if not (isinstance(ctx_tokens, int) or (isinstance(api, dict) and api)):
            self.log_system('No snapshot/token statistics available for this thread yet; send a message first.')
            return

        if not isinstance(api, dict):
            api = {}

        ti = api.get('total_input_tokens') or 0
        to = api.get('total_output_tokens') or 0
        cached_ctx = api.get('cached_tokens') or 0
        cached_in = api.get('cached_input_tokens') or 0
        calls = api.get('approx_call_count') or 0

        def _fmt_tok(n: int) -> str:
            try:
                n = int(n)
            except Exception:
                return str(n)
            if n < 1000:
                return str(n)
            return f"{n/1000:.2f}k"

        lines: List[str] = []
        lines.append(f"Thread {self.current_thread[-8:]} token usage:")
        if isinstance(ctx_tokens, int):
            lines.append(f"  context_tokens:        {ctx_tokens} ({_fmt_tok(ctx_tokens)})")
        else:
            lines.append(f"  context_tokens:        (n/a)")
        lines.append(f"  total_input_tokens:    {ti} ({_fmt_tok(ti)})")
        lines.append(f"  cached_input_tokens:   {cached_in} ({_fmt_tok(cached_in)})")
        lines.append(f"  cached_tokens (last):  {cached_ctx} ({_fmt_tok(cached_ctx)})")
        lines.append(f"  total_output_tokens:   {to} ({_fmt_tok(to)})")
        lines.append(f"  approx_call_count:     {calls}")

        # Cost breakdown (computed by eggthreads.total_token_stats).
        cost_lines: List[str] = []

        # total_token_stats() attaches both per-model usage (tokens) and
        # per-model cost breakdown (USD) onto api_usage.
        by_model_usage = api.get('by_model') if isinstance(api.get('by_model'), dict) else {}
        cu = api.get('cost_usd') if isinstance(api.get('cost_usd'), dict) else {}
        total_cost = float(cu.get('total') or 0.0)
        by_model_cost = cu.get('by_model') if isinstance(cu.get('by_model'), dict) else {}
        warnings = cu.get('warnings') if isinstance(cu.get('warnings'), list) else []

        cost_lines.append("")
        cost_lines.append("Approximate cost (USD):")
        cost_lines.append(f"  total:   ${total_cost:.4f}")

        # Per-model breakdown: show tokens + cost.
        if by_model_usage or by_model_cost:
            cost_lines.append("")
            cost_lines.append("Per-model breakdown:")

            def _model_sort_key(mk: str) -> tuple:
                # Highest cost first, then name.
                try:
                    ctot = float((by_model_cost.get(mk) or {}).get('total') or 0.0)
                except Exception:
                    ctot = 0.0
                return (-ctot, mk)

            model_keys: set[str] = set()
            for mk in (by_model_usage.keys() if isinstance(by_model_usage, dict) else []):
                if isinstance(mk, str):
                    model_keys.add(mk)
            for mk in (by_model_cost.keys() if isinstance(by_model_cost, dict) else []):
                if isinstance(mk, str):
                    model_keys.add(mk)

            for mk in sorted(model_keys, key=_model_sort_key):
                u = by_model_usage.get(mk) if isinstance(by_model_usage, dict) else {}
                c = by_model_cost.get(mk) if isinstance(by_model_cost, dict) else {}
                if not isinstance(u, dict):
                    u = {}
                if not isinstance(c, dict):
                    c = {}

                try:
                    mcalls = int(u.get('approx_call_count') or 0)
                except Exception:
                    mcalls = 0
                try:
                    tin = int(u.get('total_input_tokens') or 0)
                except Exception:
                    tin = 0
                try:
                    tcached = int(u.get('cached_input_tokens') or 0)
                except Exception:
                    tcached = 0
                try:
                    tout = int(u.get('total_output_tokens') or 0)
                except Exception:
                    tout = 0

                try:
                    cin = float(c.get('input') or 0.0)
                    ccached = float(c.get('cached') or 0.0)
                    cout = float(c.get('output') or 0.0)
                    ctot = float(c.get('total') or 0.0)
                except Exception:
                    cin = ccached = cout = ctot = 0.0

                cost_lines.append(f"  {mk}:")
                cost_lines.append(
                    f"    calls={mcalls}  in={tin}({_fmt_tok(tin)})  cached_in={tcached}({_fmt_tok(tcached)})  out={tout}({_fmt_tok(tout)})"
                )
                cost_lines.append(
                    f"    cost: input=${cin:.4f}  cached=${ccached:.4f}  output=${cout:.4f}  total=${ctot:.4f}"
                )

        if warnings:
            cost_lines.append("")
            for w in warnings[:10]:
                cost_lines.append(f"  note: {w}")

        block = "\n".join(lines + cost_lines)
        self.log_system('Token usage / cost for current thread (see console for full details).')
        self.console_print_block('Cost', block, border_style='green')

    def cmd_setContextLimit(self, arg: str) -> None:
        """Handle /setContextLimit command - set max context tokens for this thread."""
        arg = (arg or '').strip()

        if not arg:
            # Show current limit
            current = get_context_limit(self.db, self.current_thread)
            if current:
                self.log_system(f"Current context limit: {current:,} tokens")
            else:
                self.log_system("No context limit set (unlimited)")
            return

        # Parse and set limit
        try:
            limit = int(arg)
            if limit <= 0:
                self.log_system("Context limit must be a positive integer")
                return

            set_context_limit(self.db, self.current_thread, limit, reason="ui /setContextLimit")
            self.log_system(f"Context limit set to {limit:,} tokens")
        except ValueError:
            self.log_system(f"Invalid number: {arg}")
            self.log_system("Usage: /setContextLimit <max_tokens>")
