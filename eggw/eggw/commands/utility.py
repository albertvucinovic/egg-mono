"""Utility commands for eggw backend."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from eggthreads import (
    approve_tool_calls_for_thread,
    thread_token_stats,
    execute_bash_command,
    list_threads,
    get_thread_auto_approval_status,
    set_context_limit,
    get_context_limit,
    get_thread_scheduling,
    get_thread_recovery,
    set_thread_scheduling,
    set_thread_recovery,
    UNSET,
    parse_args,
    append_message,
    create_snapshot,
)
from eggthreads.builtin_plugins.diagnostics import format_cost_report
from eggthreads.command_catalog import create_default_command_registry, render_command_registry_help

from ..models import CommandResponse
from .. import core
from ..core import ensure_scheduler_for

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


def _find_searxng_dir() -> Optional[Path]:
    """Find the packaged SearXNG docker-compose directory."""
    candidates = [
        Path.cwd() / "eggthreads" / "eggthreads" / "web" / "searxng",
        Path(__file__).resolve().parents[3] / "eggthreads" / "eggthreads" / "web" / "searxng",
        Path(__file__).resolve().parents[4] / "eggthreads" / "eggthreads" / "web" / "searxng",
    ]
    for candidate in candidates:
        if (candidate / "docker-compose.yml").exists():
            return candidate
    return None


def _resolve_compose_cmd() -> Optional[List[str]]:
    """Return docker compose argv prefix, or None when unavailable."""
    try:
        proc = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return ["docker", "compose"]
    except Exception:
        pass
    try:
        proc = subprocess.run(["docker-compose", "version"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return ["docker-compose"]
    except Exception:
        pass
    return None


def _run_searxng_compose(compose_args: List[str], *, action: str, success_summary: str, timeout_sec: int) -> CommandResponse:
    searxng_dir = _find_searxng_dir()
    if searxng_dir is None:
        return CommandResponse(
            success=False,
            message="Could not locate eggthreads/eggthreads/web/searxng/docker-compose.yml.",
        )
    compose = _resolve_compose_cmd()
    if compose is None:
        return CommandResponse(
            success=False,
            message="Neither 'docker compose' nor 'docker-compose' is available on PATH.",
        )
    argv = compose + compose_args
    try:
        proc = subprocess.run(
            argv,
            cwd=str(searxng_dir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return CommandResponse(
            success=False,
            message=f"SearXNG {action} timed out after {timeout_sec}s running `{' '.join(argv)}`.",
        )
    except Exception as e:
        return CommandResponse(
            success=False,
            message=f"SearXNG {action} failed to launch `{' '.join(argv)}`: {e}",
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part) or "(no output)"
    if len(combined) > 4000:
        combined = combined[:4000] + "\n... (truncated)"
    if proc.returncode != 0:
        return CommandResponse(
            success=False,
            message=f"SearXNG {action} failed (exit {proc.returncode}).\n\n$ {' '.join(argv)}\n{combined}",
            data={"returncode": proc.returncode, "cwd": str(searxng_dir)},
        )
    return CommandResponse(
        success=True,
        message=f"{success_summary}\n\n$ {' '.join(argv)}\n{combined}",
        data={"returncode": proc.returncode, "cwd": str(searxng_dir)},
    )


def cmd_start_searxng() -> CommandResponse:
    """Start local SearXNG docker-compose service."""
    return _run_searxng_compose(
        ["up", "-d"],
        action="start",
        success_summary=(
            "Container up at http://localhost:8888. web_search can now use "
            "SearXNG as the local/no-key search fallback. In auto mode, "
            "fetch_url uses Tavily Extract when configured, otherwise direct HTTP."
        ),
        timeout_sec=600,
    )


def cmd_stop_searxng() -> CommandResponse:
    """Stop local SearXNG docker-compose service."""
    return _run_searxng_compose(
        ["down"],
        action="stop",
        success_summary=(
            "Container stopped. web_search needs SearXNG restarted unless a "
            "hosted search backend is configured; fetch_url is unaffected unless "
            "explicitly pinned to Tavily without TAVILY_API_KEY."
        ),
        timeout_sec=120,
    )


def get_auto_approval_status(thread_id: str) -> bool:
    """Check if auto-approval is currently active for a thread.

    This scans the tool_call.approval events to find the current state.
    """
    if not core.db:
        return False
    return bool(get_thread_auto_approval_status(core.db, thread_id))


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


def _parse_bool_arg(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    low = value.strip().lower()
    if low in {"on", "true", "1"}:
        return True
    if low in {"off", "false", "0"}:
        return False
    return None


async def cmd_toggle_auto_continue_on_error(thread_id: str, arg: str = "") -> CommandResponse:
    """Handle /toggleAutoContinueOnError command."""

    args = parse_args(arg or "")
    raw = args.positional_or(0)
    if raw is None:
        raw = args.get("enabled") or args.get("value")

    current = get_thread_recovery(core.db, thread_id).auto_continue_on_error
    requested = _parse_bool_arg(raw)
    if raw is not None and requested is None:
        return CommandResponse(
            success=False,
            message="Usage: /toggleAutoContinueOnError [on|off|true|false|1|0]",
        )

    new_state = (not current) if requested is None else requested
    set_thread_recovery(core.db, thread_id, auto_continue_on_error=new_state)
    return CommandResponse(
        success=True,
        message=f"Auto-continue on error {'enabled' if new_state else 'disabled'}",
        data={"autoContinueOnError": new_state},
    )


async def cmd_skills(thread_id: str, query: str) -> CommandResponse:
    """List or search packaged skill documents."""
    try:
        from eggthreads.tools import create_default_tools

        search = (query or "").strip()
        args = {"query": search} if search else {}
        text = create_default_tools().execute("skill", args)
        return CommandResponse(
            success=True,
            message=text,
            data={"query": search, "action": "list_skills"},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/skills error: {e}")


async def cmd_skill(thread_id: str, name: str) -> CommandResponse:
    """Show and load a packaged skill document into thread context."""
    skill_name = (name or "").strip()
    if not skill_name:
        return CommandResponse(success=False, message="Usage: /skill <name>")

    try:
        from eggthreads.skills import get_skill
        from eggthreads.tools import create_default_tools

        skill = get_skill(skill_name)
        text = create_default_tools().execute("skill", {"name": skill.name})
        marker = f"<!-- egg-skill:{skill.name} -->"
        context_text = f"{marker}\n{text}"

        already_loaded = False
        try:
            cur = core.db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create'",
                (thread_id,),
            )
            for row in cur.fetchall():
                try:
                    payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                except Exception:
                    payload = {}
                if payload.get("role") == "system" and marker in str(payload.get("content") or ""):
                    already_loaded = True
                    break
        except Exception:
            already_loaded = False

        loaded = False
        if not already_loaded:
            append_message(core.db, thread_id, "system", context_text)
            create_snapshot(core.db, thread_id)
            loaded = True

        status = (
            f"Skill /{skill.name} loaded into thread context."
            if loaded
            else f"Skill /{skill.name} already loaded; showing document."
        )
        return CommandResponse(
            success=True,
            message=f"{status}\n\n{text}",
            data={
                "skill": skill.name,
                "title": skill.title,
                "loaded": loaded,
                "already_loaded": already_loaded,
            },
        )
    except KeyError:
        return CommandResponse(success=False, message=f"Unknown skill: {skill_name}")
    except Exception as e:
        return CommandResponse(success=False, message=f"/skill error: {e}")


async def cmd_cost(thread_id: str) -> CommandResponse:
    """Handle /cost command - show token usage and cost."""
    stats = thread_token_stats(core.db, thread_id, llm=core.llm_client)
    api = stats.get("api_usage", stats)
    ctx_tokens = stats.get("context_tokens")

    if not (isinstance(ctx_tokens, int) or (isinstance(api, dict) and api)):
        return CommandResponse(
            success=False,
            message="No snapshot/token statistics available for this thread yet; send a message first."
        )
    if not isinstance(api, dict):
        api = {}

    ti = api.get("total_input_tokens", 0) or 0
    to = api.get("total_output_tokens", 0) or 0
    tr = api.get("total_reasoning_tokens", 0) or 0
    cached_last = api.get("cached_tokens", 0) or 0  # Most recent call
    cached_total = api.get("cached_input_tokens", 0) or 0  # Total across all calls
    cache_creation_in = api.get("cache_creation_input_tokens", 0) or 0
    calls = api.get("approx_call_count", 0) or 0
    actual_calls = api.get("actual_call_count", 0) or 0
    estimated_calls = api.get("estimated_call_count")
    if estimated_calls is None:
        estimated_calls = max(int(calls) - int(actual_calls), 0)

    cu = api.get("cost_usd", {}) if isinstance(api.get("cost_usd"), dict) else {}
    total_cost = float(cu.get("total", 0) or 0)
    full_thread_tokens = stats.get("full_thread_tokens", ctx_tokens if isinstance(ctx_tokens, int) else 0) or 0
    compacted_away_tokens = max(0, int(full_thread_tokens) - int(ctx_tokens or 0))

    return CommandResponse(
        success=True,
        message=format_cost_report(stats, thread_id),
        data={
            "context_tokens": ctx_tokens,
            "full_thread_tokens": full_thread_tokens,
            "current_provider_context_tokens": ctx_tokens,
            "full_thread_context_tokens": full_thread_tokens,
            "compacted_away_tokens": compacted_away_tokens,
            "input_tokens": ti,
            "output_tokens": to,
            "reasoning_tokens": tr,
            "cached_input_tokens": cached_total,
            "cached_tokens_last": cached_last,
            "cache_creation_input_tokens": cache_creation_in,
            "approx_call_count": calls,
            "actual_call_count": actual_calls,
            "estimated_call_count": estimated_calls,
            "cost_usd": total_cost,
            "api_usage": api,
            "api_usage_since_compaction": stats.get("api_usage_since_compaction") if isinstance(stats.get("api_usage_since_compaction"), dict) else None,
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
    from eggthreads import wait_subtree_idle

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
    help_text = render_command_registry_help(create_default_command_registry())
    help_text += """

EggW-only commands:
    /rename <name> — Rename the current thread.
    /theme [name] — List or switch browser themes.

EggW behavior notes:
    /redraw — No-op in EggW; the browser UI updates automatically.
    /displayMode — Terminal-only; EggW uses the browser layout.

Shell:
    $ <command> — Run visible bash command.
    $$ <command> — Run hidden bash command."""

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



def cmd_display_verbosity(level_arg: str) -> CommandResponse:
    """Handle /displayVerbosity command - set transcript verbosity (frontend-only)."""
    level = level_arg.strip().lower()
    allowed = {"max", "medium", "min"}
    if not level:
        return CommandResponse(
            success=True,
            message="Usage: /displayVerbosity <max|medium|min>",
            data={"action": "display_verbosity_usage"},
        )
    if level not in allowed:
        return CommandResponse(
            success=False,
            message="Usage: /displayVerbosity <max|medium|min>",
        )
    return CommandResponse(
        success=True,
        message=f"Display verbosity set to {level}.",
        data={"action": "set_display_verbosity", "display_verbosity": level},
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


def cmd_reload(thread_id: str) -> CommandResponse:
    """Handle /reload command - restart eggw.sh and reopen current thread."""
    state_file = os.environ.get("EGGW_RELOAD_STATE_FILE")
    if not state_file:
        return CommandResponse(
            success=False,
            message="/reload is only available when launched via eggw.sh",
        )

    try:
        Path(state_file).write_text(f"{thread_id}\n", encoding="utf-8")
    except Exception as e:
        return CommandResponse(
            success=False,
            message=f"/reload failed to save thread id: {e}",
        )

    async def _exit_for_reload() -> None:
        await asyncio.sleep(0.1)
        os._exit(int(os.environ.get("EGGW_RELOAD_EXIT_CODE", "75")))

    try:
        asyncio.create_task(_exit_for_reload())
    except Exception as e:
        return CommandResponse(
            success=False,
            message=f"/reload failed to schedule restart: {e}",
        )

    return CommandResponse(
        success=True,
        message="Reloading eggw and reopening this thread...",
        data={"action": "reload", "thread_id": thread_id},
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


async def cmd_setContextLimit(thread_id: str, arg: str = "") -> CommandResponse:
    """Handle /setContextLimit command - set max context tokens for thread."""
    arg = (arg or '').strip()

    def fmt_tok(n: int) -> str:
        if n < 1000:
            return str(n)
        return f"{n/1000:.1f}k"

    if not arg:
        # Show current limit and context usage
        current_limit = get_context_limit(core.db, thread_id)
        stats = thread_token_stats(core.db, thread_id)
        current_tokens = stats.get('context_tokens', 0)

        lines = [
            f"Thread {thread_id[-8:]} context limit:",
            "",
            f"  current_tokens:  {current_tokens:,} ({fmt_tok(current_tokens)})",
        ]

        if current_limit:
            pct = (current_tokens / current_limit * 100) if current_limit > 0 else 0
            remaining = max(0, current_limit - current_tokens)
            lines.extend([
                f"  context_limit:   {current_limit:,} ({fmt_tok(current_limit)})",
                f"  usage:           {pct:.1f}%",
                f"  remaining:       {remaining:,} ({fmt_tok(remaining)})",
            ])
        else:
            lines.append(f"  context_limit:   (unlimited)")

        lines.extend(["", "Usage: /setContextLimit <max_tokens>"])

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "context_limit": current_limit,
                "current_tokens": current_tokens,
                "usage_percent": (current_tokens / current_limit * 100) if current_limit else None,
            }
        )

    # Parse and set limit
    try:
        limit = int(arg)
        if limit <= 0:
            return CommandResponse(
                success=False,
                message="Context limit must be a positive integer"
            )

        set_context_limit(core.db, thread_id, limit, reason="ui /setContextLimit")

        # Show updated status
        stats = thread_token_stats(core.db, thread_id)
        current_tokens = stats.get('context_tokens', 0)
        pct = (current_tokens / limit * 100) if limit > 0 else 0

        lines = [
            f"Thread {thread_id[-8:]} context limit updated:",
            "",
            f"  current_tokens:  {current_tokens:,} ({fmt_tok(current_tokens)})",
            f"  context_limit:   {limit:,} ({fmt_tok(limit)})",
            f"  usage:           {pct:.1f}%",
        ]

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "context_limit": limit,
                "current_tokens": current_tokens,
                "usage_percent": pct,
            }
        )
    except ValueError:
        return CommandResponse(
            success=False,
            message=f"Invalid number: {arg}. Usage: /setContextLimit <max_tokens>"
        )


async def cmd_setThreadPriority(thread_id: str, arg: str = "") -> CommandResponse:
    """Handle /setThreadPriority command - set scheduling settings.

    Syntax: /setThreadPriority thread=<id> priority=<int> threshold=<seconds> apiTimeout=<seconds>
    All parameters optional. apiTimeout=0 or -1 means no timeout.
    Use empty value or "unset" to reset to default.
    """
    args = parse_args(arg or '')
    target_thread = args.get('thread', thread_id)

    def parse_with_unset(key, converter):
        raw = args.get(key)
        if raw is None:
            return None
        if raw == '' or raw.lower() == 'unset':
            return UNSET
        try:
            return converter(raw)
        except (ValueError, TypeError):
            return None

    new_priority = parse_with_unset('priority', int)
    new_threshold = parse_with_unset('threshold', float)
    new_api_timeout = parse_with_unset('apiTimeout', float)

    # If no action params, show current values
    if new_priority is None and new_threshold is None and new_api_timeout is None:
        settings = get_thread_scheduling(core.db, target_thread)
        threshold_str = f"{settings.threshold}s" if settings.threshold is not None else "default (global)"
        api_timeout_str = "no timeout" if settings.api_timeout is not None and settings.api_timeout <= 0 else \
                          f"{settings.api_timeout}s" if settings.api_timeout is not None else "default (600s)"

        return CommandResponse(
            success=True,
            message=f"Thread Scheduling Settings\n\n"
                    f"  Thread: {target_thread[-8:]}\n"
                    f"  Priority: {settings.priority}\n"
                    f"  Sticky threshold: {threshold_str}\n"
                    f"  API timeout: {api_timeout_str}\n\n"
                    f"  Usage: /setThreadPriority priority=<int> threshold=<seconds> apiTimeout=<seconds>\n"
                    f"  Use empty value or 'unset' to reset to default",
            data={
                "thread_id": target_thread,
                "priority": settings.priority,
                "threshold": settings.threshold,
                "apiTimeout": settings.api_timeout,
            }
        )

    # Set values
    set_thread_scheduling(
        core.db, target_thread,
        priority=new_priority,
        threshold=new_threshold,
        api_timeout=new_api_timeout,
    )

    # Build confirmation
    messages = []
    data = {"thread_id": target_thread}
    if isinstance(new_priority, type(UNSET)):
        messages.append("Priority reset to default (0)")
        data["priority"] = 0
    elif new_priority is not None:
        messages.append(f"Priority set to {new_priority}")
        data["priority"] = new_priority
    if isinstance(new_threshold, type(UNSET)):
        messages.append("Sticky threshold reset to default")
        data["threshold"] = None
    elif new_threshold is not None:
        messages.append(f"Sticky threshold set to {new_threshold}s")
        data["threshold"] = new_threshold
    if isinstance(new_api_timeout, type(UNSET)):
        messages.append("API timeout reset to default")
        data["apiTimeout"] = None
    elif new_api_timeout is not None:
        timeout_str = f"{new_api_timeout}s" if new_api_timeout > 0 else "no timeout"
        messages.append(f"API timeout set to {timeout_str}")
        data["apiTimeout"] = new_api_timeout

    return CommandResponse(
        success=True,
        message=f"Thread {target_thread[-8:]}: {', '.join(messages)}",
        data=data
    )
