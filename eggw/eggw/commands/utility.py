"""Utility commands for eggw backend."""
from __future__ import annotations

import asyncio
import json
from typing import List

from eggthreads import (
    approve_tool_calls_for_thread,
    total_token_stats,
    execute_bash_command,
    list_threads,
)

from models import CommandResponse
import core
from core import ensure_scheduler_for

# Available themes (text-colored variants first, then background variants)
THEMES = [
    # Text-colored themes (uniform background, colored text)
    "dark", "cyberpunk", "forest", "ocean", "sunset", "mono", "midnight",
    "disney", "fruit", "vegetables", "coffee", "matrix", "light", "light-mono",
    "colorful", "colorful-light",
    # Background variants (colored backgrounds)
    "dark-background", "cyberpunk-background", "forest-background", "ocean-background",
    "sunset-background", "mono-background", "midnight-background", "disney-background",
    "fruit-background", "vegetables-background", "coffee-background", "matrix-background",
    "light-background", "light-mono-background", "colorful-light-background",
]


def get_auto_approval_status(thread_id: str) -> bool:
    """Check if auto-approval is currently active for a thread.

    This scans the tool_call.approval events to find the current state.
    """
    if not core.db:
        return False

    # Scan events for global_approval/revoke_global_approval
    cur = core.db.conn.execute(
        """SELECT payload_json FROM events
           WHERE thread_id=? AND type='tool_call.approval'
           ORDER BY event_seq DESC""",
        (thread_id,)
    )

    for row in cur.fetchall():
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            decision = payload.get("decision")
            if decision == "global_approval":
                return True
            if decision == "revoke_global_approval":
                return False
        except:
            continue

    return False


async def cmd_toggle_auto_approval(thread_id: str) -> CommandResponse:
    """Handle /toggleAutoApproval command."""
    current_state = get_auto_approval_status(thread_id)
    new_state = not current_state

    # Use the appropriate decision
    decision = "global_approval" if new_state else "revoke_global_approval"
    reason = f"Auto-approval {'enabled' if new_state else 'disabled'} via web UI"

    approve_tool_calls_for_thread(core.db, thread_id, decision=decision, reason=reason)

    return CommandResponse(
        success=True,
        message=f"Auto-approval {'enabled' if new_state else 'disabled'}",
        data={"auto_approval": new_state},
    )


async def cmd_cost(thread_id: str) -> CommandResponse:
    """Handle /cost command - show token usage and cost (matches egg.py format)."""
    stats = total_token_stats(core.db, thread_id, llm=core.llm_client)
    api = stats.get("api_usage", {})
    ctx_tokens = stats.get("context_tokens", 0)

    if not api:
        return CommandResponse(
            success=False,
            message="No token statistics available for this thread yet; send a message first."
        )

    ti = api.get("total_input_tokens", 0) or 0
    to = api.get("total_output_tokens", 0) or 0
    tr = api.get("total_reasoning_tokens", 0) or 0
    cached_last = api.get("cached_tokens", 0) or 0  # Most recent call
    cached_total = api.get("cached_input_tokens", 0) or 0  # Total across all calls
    calls = api.get("approx_call_count", 0) or 0

    def fmt_tok(n: int) -> str:
        if n < 1000:
            return str(n)
        return f"{n/1000:.2f}k"

    lines = [
        f"Thread {thread_id[-8:]} token usage:",
        f"  context_tokens:        {ctx_tokens} ({fmt_tok(ctx_tokens)})",
        f"  total_input_tokens:    {ti} ({fmt_tok(ti)})",
        f"  cached_input_tokens:   {cached_total} ({fmt_tok(cached_total)})",
        f"  cached_tokens (last):  {cached_last} ({fmt_tok(cached_last)})",
        f"  total_output_tokens:   {to} ({fmt_tok(to)})",
        f"  total_reasoning_tokens: {tr} ({fmt_tok(tr)})",
        f"  approx_call_count:     {calls}",
    ]

    # Cost breakdown
    cu = api.get("cost_usd", {}) if isinstance(api.get("cost_usd"), dict) else {}
    total_cost = float(cu.get("total", 0) or 0)

    lines.append("")
    lines.append(f"Approximate cost (USD): ${total_cost:.4f}")

    # Per-model breakdown if available
    by_model_cost = cu.get("by_model", {}) if isinstance(cu.get("by_model"), dict) else {}
    by_model_usage = api.get("by_model", {}) if isinstance(api.get("by_model"), dict) else {}

    if by_model_usage or by_model_cost:
        lines.append("")
        lines.append("Per-model breakdown:")
        model_keys = set(by_model_usage.keys()) | set(by_model_cost.keys())
        for mk in sorted(model_keys, key=lambda k: -float((by_model_cost.get(k, {}).get("total") or 0))):
            u = by_model_usage.get(mk, {})
            c = by_model_cost.get(mk, {})
            m_in = u.get("total_input_tokens", 0) or 0
            m_out = u.get("total_output_tokens", 0) or 0
            m_cached = u.get("cached_input_tokens", 0) or 0
            m_cost = float(c.get("total", 0) or 0)
            lines.append(f"  {mk}: {fmt_tok(m_in)} in, {fmt_tok(m_out)} out, {fmt_tok(m_cached)} cached, ${m_cost:.4f}")

    return CommandResponse(
        success=True,
        message="\n".join(lines),
        data={
            "context_tokens": ctx_tokens,
            "input_tokens": ti,
            "output_tokens": to,
            "reasoning_tokens": tr,
            "cached_input_tokens": cached_total,
            "cached_tokens_last": cached_last,
            "approx_call_count": calls,
            "cost_usd": total_cost,
        },
    )


def cmd_schedulers() -> CommandResponse:
    """Handle /schedulers command."""
    if not core.active_schedulers:
        return CommandResponse(success=True, message="No active schedulers")

    lines = []
    for root_id in core.active_schedulers:
        lines.append(f"  {root_id[-8:]}")

    return CommandResponse(
        success=True,
        message=f"Active schedulers ({len(core.active_schedulers)}):\n" + "\n".join(lines),
        data={"count": len(core.active_schedulers), "roots": list(core.active_schedulers.keys())},
    )


async def cmd_wait_for_threads(thread_id: str, thread_selectors: str) -> CommandResponse:
    """Handle /waitForThreads command - wait for threads to complete."""
    from eggthreads import wait_subtree_idle, collect_subtree

    selectors = thread_selectors.strip().split() if thread_selectors.strip() else []
    if not selectors:
        return CommandResponse(
            success=False,
            message="Usage: /waitForThreads <thread_id> [thread_id...]",
        )

    # Resolve selectors to thread IDs
    target_ids: List[str] = []
    all_threads = list_threads(core.db)
    threads_by_id = {t.thread_id: t for t in all_threads}

    for sel in selectors:
        # Try exact match
        if sel in threads_by_id:
            target_ids.append(sel)
            continue

        # Try partial match
        matches = [t for t in all_threads if sel.lower() in t.thread_id.lower()]
        if len(matches) == 1:
            target_ids.append(matches[0].thread_id)
        elif len(matches) > 1:
            return CommandResponse(
                success=False,
                message=f"Ambiguous thread selector: {sel}",
            )
        else:
            return CommandResponse(
                success=False,
                message=f"Thread not found: {sel}",
            )

    # Wait for each thread subtree to complete
    try:
        for tid in target_ids:
            await wait_subtree_idle(core.db, tid, poll_sec=0.2, quiet_checks=3)

        return CommandResponse(
            success=True,
            message=f"All {len(target_ids)} thread(s) completed",
            data={"waited_for": target_ids},
        )
    except asyncio.CancelledError:
        return CommandResponse(
            success=False,
            message="Wait cancelled",
        )
    except Exception as e:
        return CommandResponse(
            success=False,
            message=f"/waitForThreads error: {e}",
        )


async def execute_bash_command_handler(thread_id: str, script: str, hidden: bool) -> CommandResponse:
    """Execute a bash command as a tool call."""
    if not script:
        return CommandResponse(success=False, message="Empty bash command")

    # Use eggthreads' execute_bash_command which handles everything correctly
    tc_id = execute_bash_command(core.db, thread_id, script, hidden=hidden)

    # Ensure scheduler is running
    ensure_scheduler_for(thread_id)

    return CommandResponse(
        success=True,
        message=f"Executing: {script}",
        data={"tool_call_id": tc_id, "hidden": hidden},
    )


def cmd_help() -> CommandResponse:
    """Handle /help command."""
    help_text = """Available commands:

Thread Management:
  /newThread [name]              - Create a new root thread
  /spawn <context>               - Spawn a child thread
  /thread <selector>             - Switch to a thread by ID/name/recap
  /threads                       - List all threads
  /listChildren                  - List children of current thread
  /parentThread                  - Switch to parent thread
  /deleteThread [selector]       - Delete thread (and subtree)
  /duplicateThread [name] [msg_id] - Duplicate thread
  /continue [msg_id]             - Continue thread from point
  /rename <name>                 - Rename current thread

Model:
  /model [name]                  - Get or set model for thread
  /updateAllModels <provider>    - Update model catalog

Tools:
  /toolsOn                       - Enable all tools
  /toolsOff                      - Disable all tools
  /toolsStatus                   - Show tools status
  /disableTool <name>            - Disable specific tool
  /enableTool <name>             - Enable specific tool
  /toolsSecrets <on|off>         - Toggle raw output

Sandbox:
  /toggleSandboxing              - Toggle sandbox on/off
  /setSandboxConfiguration <cfg> - Apply sandbox config
  /getSandboxingConfig           - Show sandbox config

Utility:
  /cost                          - Show token usage and cost
  /toggleAutoApproval            - Toggle auto-approve tools
  /schedulers                    - Show active schedulers
  /waitForThreads <ids...>       - Wait for threads to complete
  /help                          - Show this help

Shell:
  $ <command>                    - Run visible bash command
  $$ <command>                   - Run hidden bash command"""

    return CommandResponse(
        success=True,
        message=help_text,
    )


# Display-related commands (frontend-only, backend returns action signals)
def cmd_toggle_panel(panel_name: str) -> CommandResponse:
    """Handle /togglePanel command - toggle panel visibility (frontend-only)."""
    name = panel_name.strip().lower()
    valid_panels = ["chat", "children", "system"]
    if name not in valid_panels:
        return CommandResponse(
            success=True,
            message=f"Usage: /togglePanel <{'/'.join(valid_panels)}>",
        )

    return CommandResponse(
        success=True,
        message=f"Toggle panel: {name}",
        data={"panel": name, "action": "toggle"},
    )


def cmd_paste() -> CommandResponse:
    """Handle /paste command - paste from clipboard (frontend-only)."""
    return CommandResponse(
        success=True,
        message="Use Ctrl+V or Cmd+V to paste from clipboard",
        data={"action": "paste"},
    )


def cmd_enter_mode(mode: str) -> CommandResponse:
    """Handle /enterMode command - set Enter key behavior (frontend-only)."""
    mode = mode.strip().lower()
    if mode not in ("send", "newline"):
        return CommandResponse(
            success=True,
            message="Usage: /enterMode <send|newline>\n  send = Enter sends message (Shift+Enter for newline)\n  newline = Enter inserts newline (Ctrl+Enter to send)",
        )

    return CommandResponse(
        success=True,
        message=f"Enter mode set to: {mode}",
        data={"enter_mode": mode},
    )


def cmd_toggle_borders() -> CommandResponse:
    """Handle /toggleBorders command - toggle panel borders (frontend-only)."""
    return CommandResponse(
        success=True,
        message="Panel borders toggled",
        data={"action": "toggle_borders"},
    )


def cmd_quit() -> CommandResponse:
    """Handle /quit command (no-op in web UI)."""
    return CommandResponse(
        success=True,
        message="Quit command not applicable in web UI",
    )


def cmd_theme(theme_name: str) -> CommandResponse:
    """Handle /theme command - change color scheme."""
    if not theme_name:
        return CommandResponse(
            success=True,
            message=f"Available themes: {', '.join(THEMES)}\nUse /theme <name> to switch",
            data={"themes": THEMES, "action": "list_themes"},
        )

    theme = theme_name.lower().strip()
    if theme not in THEMES:
        return CommandResponse(
            success=False,
            message=f"Unknown theme: {theme}. Available: {', '.join(THEMES)}",
        )

    return CommandResponse(
        success=True,
        message=f"Theme changed to: {theme}",
        data={"theme": theme, "action": "set_theme"},
    )
