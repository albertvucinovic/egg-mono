#!/usr/bin/env python3
"""
Headless example: drive an entire subtree by running a single SubtreeScheduler on the root.
The scheduler starts a one-invoke-per-turn ThreadRunner for every runnable thread in the subtree,
continuing automatically after tool calls (new invoke_id per turn).

Run:
  python3 -u eggthreads/examples/headless_subtree_scheduler.py

Optional environment variables:
  SYSTEM_PROMPT_PATH      Path to system prompt file (default: egg/systemPrompt or systemPrompt or builtin)
  EGG_MODELS_PATH         Path to models.json (default: egg/models.json)
  EGG_ALL_MODELS_PATH     Path to all-models.json (default: egg/all-models.json)
  MAX_CONCURRENT          Concurrency limit for scheduler (default: 8)
"""
import os
import sys
import json
from pathlib import Path
import asyncio
from typing import Any, Dict, List

# Allow running this file directly: add project root to sys.path
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except Exception:
    pass

from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    create_root_thread,
    create_child_thread,
    append_message,
    create_snapshot,
    is_thread_runnable,
    set_subtree_tools_enabled,
    total_token_stats,
)

SYSTEM_PROMPT_DEFAULT = "You are a helpful assistant."


def _env_path(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v and isinstance(v, str) and v.strip():
        return v.strip()
    return default


def load_system_prompt() -> str:
    # Prefer explicit env var
    p = os.environ.get("SYSTEM_PROMPT_PATH")
    if p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except Exception:
            pass
    promptPath = f"{os.getcwd()}/systemPrompt"
    if os.path.exists(promptPath):
        try:
            with open(promptPath, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except Exception:
            pass
    return SYSTEM_PROMPT_DEFAULT


def collect_subtree(db: ThreadsDB, root_id: str) -> List[str]:
    out: List[str] = []
    q: List[str] = [root_id]
    seen = set()
    while q:
        t = q.pop(0)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        cur = db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (t,))
        for row in cur.fetchall():
            q.append(row[0])
    return out


def _thread_token_report(db: ThreadsDB, tid: str, *, llm=None) -> tuple[int, int, float]:
    """Return (context_tokens, approx_llm_call_count, cost_usd_total).

    Uses eggthreads.total_token_stats() so counts increase during streaming.
    """

    ts = total_token_stats(db, tid, llm=llm)
    try:
        ctx = int(ts.get('context_tokens') or 0)
    except Exception:
        ctx = 0
    api = ts.get('api_usage') if isinstance(ts.get('api_usage'), dict) else {}
    try:
        calls = int(api.get('approx_call_count') or 0)
    except Exception:
        calls = 0
    cost = 0.0
    try:
        cu = api.get('cost_usd') if isinstance(api.get('cost_usd'), dict) else {}
        cost = float(cu.get('total') or 0.0)
    except Exception:
        cost = 0.0
    return (ctx, calls, cost)

def list_active_threads(db: ThreadsDB, subtree: List[str]) -> List[str]:
    active: List[str] = []
    for tid in subtree:
        row_open = None
        try:
            row_open = db.current_open(tid)
        except Exception:
            row_open = None
        if row_open is not None or is_thread_runnable(db, tid):
            active.append(tid)
    return active


async def periodic_reporter(db: ThreadsDB, root_id: str, interval_sec: float = 2.0, *, llm=None) -> None:
    """Every interval_sec, print summary of active threads and token counts.

    The token counts are computed by eggthreads.snapshot_token_stats and
    embedded into each thread snapshot by create_snapshot().
    """
    while True:
        try:
            await asyncio.sleep(interval_sec)
            
            # Create snapshots for all children threads before reporting
            subtree = collect_subtree(db, root_id)
            children = [t for t in subtree if t != root_id]
            active = list_active_threads(db, children)
            
            # Refresh snapshots only when there are *new messages*.
            #
            # Stream deltas (stream.delta) can arrive at very high
            # frequency while a thread is running. Rebuilding snapshots
            # for every delta is expensive and unnecessary for live
            # monitoring, because total_token_stats() accounts for the
            # streaming tail directly.
            for tid in children:
                try:
                    th = db.get_thread(tid)
                    last_snap = int(th.snapshot_last_event_seq) if th else -1
                    # Only rebuild if there is at least one msg.create
                    # beyond the last snapshot.
                    row = db.conn.execute(
                        "SELECT 1 FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>? LIMIT 1",
                        (tid, last_snap),
                    ).fetchone()
                    if row is not None:
                        create_snapshot(db, tid)
                except Exception as e:
                    print(f"[status] failed to snapshot {tid[-8:]}: {e}")
            
            # Now do the report
            stats = {tid: _thread_token_report(db, tid, llm=llm) for tid in children}

            active_summary = ", ".join(
                f"{tid[-8:]}({stats.get(tid, (0, 0, 0.0))[0]}t,{stats.get(tid, (0, 0, 0.0))[1]}c,${stats.get(tid, (0, 0, 0.0))[2]:.4f})"
                for tid in active
            ) or "-"
            total_ctx_tokens = sum(ctx for (ctx, _calls, _cost) in stats.values())
            total_cost = sum(cost for (_ctx, _calls, cost) in stats.values())
            print(
                f"[status] active {len(active)}/{len(children)} | "
                f"total_ctx_tokens={total_ctx_tokens} | total_cost≈${total_cost:.4f} | active: {active_summary}"
            )
        except Exception as e:
            print(f"[status] reporter error: {e}")


async def wait_subtree_idle(db: ThreadsDB, root_id: str, poll_sec: float = 0.1, quiet_checks: int = 3) -> None:
    # Wait until no runnable threads remain in the subtree for a number of consecutive checks
    subtree = collect_subtree(db, root_id)
    stable = 0
    while True:
        any_run = False
        for tid in subtree:
            # Treat "currently streaming" as running even if no new RA is
            # discoverable yet.
            if db.current_open(tid) is not None or is_thread_runnable(db, tid):
                any_run = True
                break
        if any_run:
            stable = 0
        else:
            stable += 1
            if stable >= quiet_checks:
                return
        await asyncio.sleep(poll_sec)


async def main():
    db = ThreadsDB()
    db.init_schema()

    system_prompt = load_system_prompt()
    print("System prompt:")
    print(system_prompt)
    models_path = _env_path("EGG_MODELS_PATH", "models.json")
    all_models_path = _env_path("EGG_ALL_MODELS_PATH", "all-models.json")
    print(f"models_path={models_path}")

    # Create root and children with tasks
    root_id = create_root_thread(db, name="Batch Root")
    num_tasks = 5
    tasks = [
        f"Write a story named story_#{i} into a file story_#{i}.md . It should be at least 400 words long."
        for i in range(1, num_tasks + 1)
    ]

    for i, task in enumerate(tasks, start=1):
        child = create_child_thread(db, root_id, name=f"agent-{i:03d}")#, initial_model_key="baseten:Openai-120b")
        append_message(db, child, "system", system_prompt)
        append_message(db, child, "user", task)
        create_snapshot(db, child)

    # Allow threads in this subtree to use tools for this batch run.
    # This ensures that, even if a global default disables tools, each
    # agent may perform tool-assisted reasoning (bash/python/search,
    # etc.) for its work during this single scheduler invocation.
    try:
        set_subtree_tools_enabled(db, root_id, True)
    except Exception as e:
        print(f"[status] warning: failed to enable tools for subtree: {e}")

    # Also auto-approve tool calls *for the first user turn* in this
    # batch so that assistant-originated tools do not get stuck
    # waiting for interactive approval (e.g. from an attached TUI).
    #
    # We emit a single ``tool_call.approval`` event with decision
    # "all-in-turn" for each thread; build_tool_call_states will then
    # auto-grant TC1 approvals for tool calls whose parent assistant
    # messages fall between that thread's last user message before the
    # event and the next user message. Since this example seeds one
    # user message per child thread and does not add more, this
    # effectively gives each agent a *single* tool-using turn.
    try:
        import os as _os
        subtree_ids = collect_subtree(db, root_id)
        for tid in subtree_ids:
            db.append_event(
                event_id=_os.urandom(10).hex(),
                thread_id=tid,
                type_='tool_call.approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'decision': 'all-in-turn',
                    'reason': 'headless_subtree_scheduler: auto-approve tools for the initial user turn',
                },
            )
    except Exception as e:
        print(f"[status] warning: failed to enable per-turn tool auto-approval: {e}")

    # Start a scheduler for the entire subtree rooted at 'root_id'. We
    # construct RunnerConfig via the public eggthreads namespace so the
    # example works both when running from source and when eggthreads is
    # installed as a package.
    max_concurrent = int(os.environ.get("MAX_CONCURRENT", "8") or "8")
    # RunnerConfig is part of the public eggthreads API.
    # (If you are running from a source checkout, ensure eggthreads/__init__.py
    # re-exports it.)
    from eggthreads import RunnerConfig

    cfg = RunnerConfig(max_concurrent_threads=max_concurrent)
    scheduler = SubtreeScheduler(db, root_thread_id=root_id, config=cfg, models_path=models_path, all_models_path=all_models_path)

    # Use the scheduler's eggllm client for cost lookups.
    llm_client = getattr(scheduler, 'llm', None)

    sched_task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    report_task = asyncio.create_task(periodic_reporter(db, root_id, 2.0, llm=llm_client))
    print("Created tasks!")

    # Wait until subtree becomes idle (no runnable threads)
    await wait_subtree_idle(db, root_id)

    # Cancel scheduler task now that we are idle
    try:
        sched_task.cancel()
        await asyncio.sleep(0)  # allow cancellation to propagate
    except Exception:
        pass
    try:
        report_task.cancel()
        await asyncio.sleep(0)
    except Exception:
        pass

    # Print short recaps
    print("\nAll agents finished. Summaries:")
    subtree = collect_subtree(db, root_id)
    for tid in subtree:
        row = db.get_thread(tid)
        if row and row.thread_id != root_id:
            recap = (row.short_recap or "").strip()
            print(f"{tid[-8:]}: {recap or '(no short_recap found)'}")


if __name__ == "__main__":
    asyncio.run(main())
