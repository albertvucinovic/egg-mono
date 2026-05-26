from __future__ import annotations

"""Built-in diagnostic/status commands."""

from dataclasses import dataclass
from typing import Any, Dict, List

from ..plugins import PluginContext
from ..token_count import thread_token_stats


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _print_block(context: Any, title: str, text: str, *, border_style: str = "cyan") -> None:
    if context.console_print_block is not None:
        context.console_print_block(title, text, border_style=border_style)
    else:
        _log(context, text)


def _target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def schedulers_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    app = getattr(context, "app", None)
    active = getattr(app, "active_schedulers", {}) if app is not None else {}
    if not active:
        _log(context, "No active schedulers in this session.")
        return CommandResult(clear_input=True)
    out: List[str] = []
    formatter = context.format_threads or getattr(app, "format_tree", None)
    for rid in active.keys():
        out.append(f"- root {rid[-8:]}")
        if formatter is not None:
            out.append(formatter(rid))
    block = "\n".join(out)
    _log(context, "Active SubtreeSchedulers (see console for full).")
    _print_block(context, "Schedulers", block, border_style="cyan")
    return CommandResult(clear_input=True)


def cost_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context, "cost")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        llm = context.llm_client if getattr(context, "llm_client", None) is not None else getattr(getattr(context, "app", None), "llm_client", None)
        stats = thread_token_stats(db, thread_id, llm=llm)
        ctx_tokens = stats.get("context_tokens")
        full_thread_tokens = stats.get("full_thread_tokens")
        api = stats.get("api_usage", stats)
        since_api = stats.get("api_usage_since_compaction")
    except Exception as e:
        _log(context, f"/cost error: {e}")
        return CommandResult(clear_input=False)
    if not (isinstance(ctx_tokens, int) or (isinstance(api, dict) and api)):
        _log(context, "No snapshot/token statistics available for this thread yet; send a message first.")
        return CommandResult(clear_input=False)
    if not isinstance(api, dict):
        api = {}
    if not isinstance(since_api, dict):
        since_api = None

    target_thread = context.current_thread or thread_id
    compacted_away_tokens = None
    if isinstance(ctx_tokens, int) and isinstance(full_thread_tokens, int):
        compacted_away_tokens = max(0, full_thread_tokens - ctx_tokens)

    def _fmt_tok(n: int) -> str:
        try:
            n = int(n)
        except Exception:
            return str(n)
        return str(n) if n < 1000 else f"{n/1000:.2f}k"

    def _append_usage_section(lines: List[str], title: str, usage: Dict[str, Any]) -> None:
        ti = usage.get("total_input_tokens") or 0
        to = usage.get("total_output_tokens") or 0
        cached_ctx = usage.get("cached_tokens") or 0
        cached_in = usage.get("cached_input_tokens") or 0
        calls = usage.get("approx_call_count") or 0
        lines.append(title)
        lines.append(f"  total_input_tokens:    {ti} ({_fmt_tok(ti)})")
        lines.append(f"  cached_input_tokens:   {cached_in} ({_fmt_tok(cached_in)})")
        lines.append(f"  cached_tokens (last):  {cached_ctx} ({_fmt_tok(cached_ctx)})")
        lines.append(f"  total_output_tokens:   {to} ({_fmt_tok(to)})")
        lines.append(f"  approx_call_count:     {calls}")
        cu = usage.get("cost_usd") if isinstance(usage.get("cost_usd"), dict) else {}
        if cu:
            total_cost = float(cu.get("total") or 0.0)
            lines.append(f"  cost_total_usd:        ${total_cost:.4f}")

    lines: List[str] = [f"Thread {str(target_thread)[-8:]} token usage:"]
    if isinstance(full_thread_tokens, int):
        lines.append(f"  full_thread_context_tokens:       {full_thread_tokens} ({_fmt_tok(full_thread_tokens)})")
    if isinstance(ctx_tokens, int):
        lines.append(f"  current_provider_context_tokens:  {ctx_tokens} ({_fmt_tok(ctx_tokens)})")
    if compacted_away_tokens:
        lines.append(f"  compacted_away_tokens:            {compacted_away_tokens} ({_fmt_tok(compacted_away_tokens)})")
    lines.append("")
    _append_usage_section(lines, "Full context usage (full effective history):", api)
    if since_api is not None:
        lines.append("")
        _append_usage_section(lines, "Current provider context usage (after last compaction):", since_api)

    cost_lines: List[str] = ["", "Approximate cost (USD):"]
    cu = api.get("cost_usd") if isinstance(api.get("cost_usd"), dict) else {}
    total_cost = float(cu.get("total") or 0.0)
    cost_lines.append(f"  total:   ${total_cost:.4f}")
    by_model_usage = api.get("by_model") if isinstance(api.get("by_model"), dict) else {}
    by_model_cost = cu.get("by_model") if isinstance(cu.get("by_model"), dict) else {}
    warnings = cu.get("warnings") if isinstance(cu.get("warnings"), list) else []
    if by_model_usage or by_model_cost:
        cost_lines.append("")
        cost_lines.append("Per-model breakdown:")
        model_keys = {mk for mk in by_model_usage.keys() if isinstance(mk, str)} | {mk for mk in by_model_cost.keys() if isinstance(mk, str)}
        for mk in sorted(model_keys):
            u = by_model_usage.get(mk) if isinstance(by_model_usage.get(mk), dict) else {}
            c = by_model_cost.get(mk) if isinstance(by_model_cost.get(mk), dict) else {}
            tin = int(u.get("total_input_tokens") or 0)
            tcached = int(u.get("cached_input_tokens") or 0)
            tout = int(u.get("total_output_tokens") or 0)
            mcalls = int(u.get("approx_call_count") or 0)
            cost_lines.append(f"  {mk}:")
            cost_lines.append(f"    calls={mcalls}  in={tin}({_fmt_tok(tin)})  cached_in={tcached}({_fmt_tok(tcached)})  out={tout}({_fmt_tok(tout)})")
            cost_lines.append(
                f"    cost: input=${float(c.get('input') or 0.0):.4f}  cached=${float(c.get('cached') or 0.0):.4f}  output=${float(c.get('output') or 0.0):.4f}  total=${float(c.get('total') or 0.0):.4f}"
            )
    if warnings:
        cost_lines.append("")
        for warning in warnings[:10]:
            cost_lines.append(f"  note: {warning}")

    block = "\n".join(lines + cost_lines)
    _log(context, "Token usage / cost for current thread (see console for full details).")
    _print_block(context, "Cost", block, border_style="green")
    return CommandResult(clear_input=True)


def set_context_limit_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    from ..api import get_context_limit, set_context_limit
    from ..token_count import thread_token_stats

    target = _target(context, "setContextLimit")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    text = (arg or "").strip()

    def _fmt_tok(n: int) -> str:
        return str(n) if n < 1000 else f"{n/1000:.1f}k"

    if not text:
        current_limit = get_context_limit(db, thread_id)
        stats = thread_token_stats(db, thread_id)
        current_tokens = stats.get("context_tokens", 0)
        lines: List[str] = [f"Thread {thread_id[-8:]} context limit:", "", f"  current_tokens:  {current_tokens:,} ({_fmt_tok(current_tokens)})"]
        if current_limit:
            pct = (current_tokens / current_limit * 100) if current_limit > 0 else 0
            remaining = max(0, current_limit - current_tokens)
            lines.extend([f"  context_limit:   {current_limit:,} ({_fmt_tok(current_limit)})", f"  usage:           {pct:.1f}%", f"  remaining:       {remaining:,} ({_fmt_tok(remaining)})"])
        else:
            lines.append("  context_limit:   (unlimited)")
        lines.extend(["", "Usage: /setContextLimit <max_tokens>"])
        _log(context, "Context limit info (see console).")
        _print_block(context, "Context Limit", "\n".join(lines), border_style="cyan")
        return CommandResult(clear_input=True)
    try:
        limit = int(text)
        if limit <= 0:
            _log(context, "Context limit must be a positive integer")
            return CommandResult(clear_input=False)
        set_context_limit(db, thread_id, limit, reason="ui /setContextLimit")
        stats = thread_token_stats(db, thread_id)
        current_tokens = stats.get("context_tokens", 0)
        pct = (current_tokens / limit * 100) if limit > 0 else 0
        block = "\n".join([
            f"Thread {thread_id[-8:]} context limit updated:",
            "",
            f"  current_tokens:  {current_tokens:,} ({_fmt_tok(current_tokens)})",
            f"  context_limit:   {limit:,} ({_fmt_tok(limit)})",
            f"  usage:           {pct:.1f}%",
        ])
        _log(context, f"Context limit set to {limit:,} tokens.")
        _print_block(context, "Context Limit", block, border_style="cyan")
    except ValueError:
        _log(context, f"Invalid number: {text}")
        _log(context, "Usage: /setContextLimit <max_tokens>")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def set_thread_priority_command(context: Any, arg: str):
    from ..api import UNSET, get_thread_scheduling, set_thread_scheduling
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult

    target = _target(context, "setThreadPriority")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    args = parse_args(arg or "")
    target_thread = args.get("thread", current_thread)

    def parse_with_unset(key, converter):
        raw = args.get(key)
        if raw is None:
            return None
        if raw == "" or raw.lower() == "unset":
            return UNSET
        try:
            return converter(raw)
        except (ValueError, TypeError):
            return None

    new_priority = parse_with_unset("priority", int)
    new_threshold = parse_with_unset("threshold", float)
    new_api_timeout = parse_with_unset("apiTimeout", float)
    if new_priority is None and new_threshold is None and new_api_timeout is None:
        settings = get_thread_scheduling(db, target_thread)
        threshold_str = f"{settings.threshold}s" if settings.threshold is not None else "default (global)"
        api_timeout_str = "no timeout" if settings.api_timeout is not None and settings.api_timeout <= 0 else f"{settings.api_timeout}s" if settings.api_timeout is not None else "default (600s)"
        block = "[bold]Thread Scheduling Settings[/bold]\n\n"
        block += f"  Thread: [cyan]{target_thread[-8:]}[/cyan]\n"
        block += f"  Priority: [cyan]{settings.priority}[/cyan]\n"
        block += f"  Sticky threshold: [cyan]{threshold_str}[/cyan]\n"
        block += f"  API timeout: [cyan]{api_timeout_str}[/cyan]\n\n"
        block += "  [dim]Usage: /setThreadPriority priority=<int> threshold=<seconds> apiTimeout=<seconds>[/dim]\n"
        block += "  [dim]Use empty value (e.g., threshold=) or 'unset' to reset to default[/dim]"
        _print_block(context, "Thread Priority", block, border_style="cyan")
        return CommandResult(clear_input=True)
    set_thread_scheduling(db, target_thread, priority=new_priority, threshold=new_threshold, api_timeout=new_api_timeout)
    messages: List[str] = []
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
    _log(context, f"Thread {target_thread[-8:]}: {', '.join(messages)}")
    return CommandResult(clear_input=True)


def _parse_bool_arg(value: str | None) -> bool | None:
    if value is None:
        return None
    low = value.strip().lower()
    if low in {"on", "true", "1"}:
        return True
    if low in {"off", "false", "0"}:
        return False
    return None


def toggle_auto_continue_on_error_command(context: Any, arg: str):
    from ..api import get_thread_recovery, set_thread_recovery
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult

    target = _target(context, "toggleAutoContinueOnError")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    args = parse_args(arg or "")
    raw = args.positional_or(0)
    if raw is None:
        raw = args.get("enabled") or args.get("value")

    current = get_thread_recovery(db, thread_id).auto_continue_on_error
    requested = _parse_bool_arg(raw)
    if raw is not None and requested is None:
        _log(context, "Usage: /toggleAutoContinueOnError [on|off|true|false|1|0]")
        return CommandResult(clear_input=False)

    new_state = (not current) if requested is None else requested
    set_thread_recovery(db, thread_id, auto_continue_on_error=new_state)
    state_text = "ENABLED" if new_state else "DISABLED"
    _log(context, f"Auto-continue on error {state_text} for this thread.")
    return CommandResult(clear_input=True)


def register_diagnostics_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("schedulers", schedulers_command, category="tools", usage="/schedulers", description="List active schedulers."))
    registry.register(CommandSpec("cost", cost_command, category="diagnostics", usage="/cost", description="Show token usage and approximate cost."))
    registry.register(CommandSpec("setContextLimit", set_context_limit_command, category="diagnostics", usage="/setContextLimit [limit]", description="Set or show the thread context limit."))
    registry.register(CommandSpec("setThreadPriority", set_thread_priority_command, category="diagnostics", usage="/setThreadPriority ...", description="Set thread scheduler settings."))
    registry.register(CommandSpec("toggleAutoContinueOnError", toggle_auto_continue_on_error_command, category="diagnostics", usage="/toggleAutoContinueOnError [on|off]", description="Toggle automatic continue after transient errors."))


@dataclass(frozen=True)
class DiagnosticsPlugin:
    name: str = "diagnostics"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_diagnostics_commands(context.command_registry)


__all__ = [
    "DiagnosticsPlugin",
    "cost_command",
    "register_diagnostics_commands",
    "schedulers_command",
    "set_context_limit_command",
    "set_thread_priority_command",
    "toggle_auto_continue_on_error_command",
]
