"""Utility command mixins for the egg application."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..utils import COMMANDS_TEXT, read_clipboard
from eggthreads import set_context_limit, get_context_limit, get_thread_scheduling, set_thread_scheduling, UNSET, parse_args


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
        from eggthreads import total_token_stats

        arg = (arg or '').strip()

        def _fmt_tok(n: int) -> str:
            if n < 1000:
                return str(n)
            return f"{n/1000:.1f}k"

        if not arg:
            # Show current limit and context usage
            current_limit = get_context_limit(self.db, self.current_thread)
            stats = total_token_stats(self.db, self.current_thread)
            current_tokens = stats.get('context_tokens', 0)

            lines: List[str] = []
            lines.append(f"Thread {self.current_thread[-8:]} context limit:")
            lines.append("")
            lines.append(f"  current_tokens:  {current_tokens:,} ({_fmt_tok(current_tokens)})")
            if current_limit:
                lines.append(f"  context_limit:   {current_limit:,} ({_fmt_tok(current_limit)})")
                pct = (current_tokens / current_limit * 100) if current_limit > 0 else 0
                remaining = max(0, current_limit - current_tokens)
                lines.append(f"  usage:           {pct:.1f}%")
                lines.append(f"  remaining:       {remaining:,} ({_fmt_tok(remaining)})")
            else:
                lines.append(f"  context_limit:   (unlimited)")
            lines.append("")
            lines.append("Usage: /setContextLimit <max_tokens>")

            block = "\n".join(lines)
            self.log_system('Context limit info (see console).')
            self.console_print_block('Context Limit', block, border_style='cyan')
            return

        # Parse and set limit
        try:
            limit = int(arg)
            if limit <= 0:
                self.log_system("Context limit must be a positive integer")
                return

            set_context_limit(self.db, self.current_thread, limit, reason="ui /setContextLimit")

            # Show updated status
            stats = total_token_stats(self.db, self.current_thread)
            current_tokens = stats.get('context_tokens', 0)
            pct = (current_tokens / limit * 100) if limit > 0 else 0

            lines: List[str] = []
            lines.append(f"Thread {self.current_thread[-8:]} context limit updated:")
            lines.append("")
            lines.append(f"  current_tokens:  {current_tokens:,} ({_fmt_tok(current_tokens)})")
            lines.append(f"  context_limit:   {limit:,} ({_fmt_tok(limit)})")
            lines.append(f"  usage:           {pct:.1f}%")

            block = "\n".join(lines)
            self.log_system(f'Context limit set to {limit:,} tokens.')
            self.console_print_block('Context Limit', block, border_style='cyan')
        except ValueError:
            self.log_system(f"Invalid number: {arg}")
            self.log_system("Usage: /setContextLimit <max_tokens>")

    def cmd_setThreadPriority(self, arg: str) -> None:
        """Handle /setThreadPriority command - set scheduling settings for a thread.

        Syntax: /setThreadPriority thread=<id> priority=<int> threshold=<seconds> apiTimeout=<seconds>
        All parameters are optional. apiTimeout=0 or -1 means no timeout.
        Use empty value (e.g., threshold=) or "unset" to reset to default.
        """
        args = parse_args(arg or '')
        target_thread = args.get('thread', self.current_thread)

        # Helper to parse value with "unset" support
        def parse_with_unset(key, converter):
            raw = args.get(key)
            if raw is None:
                return None  # Not specified -> keep current
            if raw == '' or raw.lower() == 'unset':
                return UNSET  # Explicitly unset
            try:
                return converter(raw)
            except (ValueError, TypeError):
                return None

        new_priority = parse_with_unset('priority', int)
        new_threshold = parse_with_unset('threshold', float)
        new_api_timeout = parse_with_unset('apiTimeout', float)

        # If no action params, show current values
        if new_priority is None and new_threshold is None and new_api_timeout is None:
            settings = get_thread_scheduling(self.db, target_thread)
            threshold_str = f"{settings.threshold}s" if settings.threshold is not None else "default (global)"
            api_timeout_str = "no timeout" if settings.api_timeout is not None and settings.api_timeout <= 0 else \
                              f"{settings.api_timeout}s" if settings.api_timeout is not None else "default (600s)"

            block = f"[bold]Thread Scheduling Settings[/bold]\n\n"
            block += f"  Thread: [cyan]{target_thread[-8:]}[/cyan]\n"
            block += f"  Priority: [cyan]{settings.priority}[/cyan]\n"
            block += f"  Sticky threshold: [cyan]{threshold_str}[/cyan]\n"
            block += f"  API timeout: [cyan]{api_timeout_str}[/cyan]\n\n"
            block += f"  [dim]Usage: /setThreadPriority priority=<int> threshold=<seconds> apiTimeout=<seconds>[/dim]\n"
            block += f"  [dim]Use empty value (e.g., threshold=) or 'unset' to reset to default[/dim]"
            self.console_print_block('Thread Priority', block, border_style='cyan')
            return

        # Set values using single API call
        set_thread_scheduling(
            self.db, target_thread,
            priority=new_priority,
            threshold=new_threshold,
            api_timeout=new_api_timeout,
        )

        # Build confirmation message
        messages = []
        if isinstance(new_priority, type(UNSET)):
            messages.append("Priority reset to default (0)")
        elif new_priority is not None:
            messages.append(f"Priority set to {new_priority}")
        if isinstance(new_threshold, type(UNSET)):
            messages.append("Sticky threshold reset to default (global)")
        elif new_threshold is not None:
            messages.append(f"Sticky threshold set to {new_threshold}s")
        if isinstance(new_api_timeout, type(UNSET)):
            messages.append("API timeout reset to default (600s)")
        elif new_api_timeout is not None:
            timeout_str = f"{new_api_timeout}s" if new_api_timeout > 0 else "no timeout"
            messages.append(f"API timeout set to {timeout_str}")

        self.log_system(f"Thread {target_thread[-8:]}: {', '.join(messages)}")
