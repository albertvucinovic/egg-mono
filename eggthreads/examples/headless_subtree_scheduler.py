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
from typing import List, Optional

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


 # use API is_thread_runnable from eggthreads


def word_count_from_snapshot(db: ThreadsDB, tid: str) -> int:
    """Count words in all string values throughout the thread snapshot, excluding JSON structure."""
    row = db.get_thread(tid)
    if not row or not row.snapshot_json:
        return 0
    try:
        snap = json.loads(row.snapshot_json)
    except Exception:
        return 0
    if not isinstance(snap, dict):
        return 0

    def count_words(obj):
        """Recursively count words in all string values."""
        total = 0
        if isinstance(obj, dict):
            for v in obj.values():
                total += count_words(v)
        elif isinstance(obj, list):
            for item in obj:
                total += count_words(item)
        elif isinstance(obj, str):
            # Simple word count - split by whitespace
            total += len(obj.split())
        return total

    return count_words(snap)





def word_count_from_events(db: ThreadsDB, tid: str) -> int:
    """Approximate real-time word count from events:
    total = words(snapshot) + words(streaming deltas for open invoke) + words(other events since snapshot).
    We count only user-visible text fields: content/text/reasoning/reason and tool/tool_call text.
    This avoids counting metadata like model_key, ids, names.
    """
    import json as _json

    def _strings_from_payload(payload: dict) -> list[str]:
        out: list[str] = []
        if not isinstance(payload, dict):
            return out
        # content/reason top-level
        for k in ('text', 'content', 'reason', 'reasoning'):
            v = payload.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        # tool streaming outputs
        tl = payload.get('tool')
        if isinstance(tl, dict):
            tv = tl.get('text')
            if isinstance(tv, str) and tv:
                out.append(tv)
        # tool-call streaming arguments
        tc = payload.get('tool_call')
        if isinstance(tc, dict):
            tv = tc.get('text')
            if isinstance(tv, str) and tv:
                out.append(tv)
        return out

    def _count_words_from_payload(payload: dict) -> int:
        total = 0
        for s in _strings_from_payload(payload):
            total += len(s.split())
        return total

    base = word_count_from_snapshot(db, tid)
    extra = 0

    # Current open stream: count its deltas (not yet folded into snapshot)
    open_row = None
    try:
        open_row = db.current_open(tid)
    except Exception:
        open_row = None
    open_invoke = (open_row["invoke_id"] if open_row else None)

    if open_invoke:
        try:
            cur = db.conn.execute(
                "SELECT payload_json FROM events WHERE invoke_id=? AND type='stream.delta' ORDER BY event_seq ASC",
                (open_invoke,),
            )
            for r in cur.fetchall():
                pj = r[0]
                try:
                    payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
                except Exception:
                    payload = {}
                extra += _count_words_from_payload(payload)
        except Exception:
            pass

    # After-snapshot events: count new words while avoiding obvious double counts
    last_seq = -1
    try:
        th = db.get_thread(tid)
        if th and isinstance(th.snapshot_last_event_seq, int):
            last_seq = th.snapshot_last_event_seq
    except Exception:
        pass

    try:
        cur = db.conn.execute(
            "SELECT type, invoke_id, payload_json FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
            (tid, last_seq),
        )
        for t, inv, pj in cur.fetchall():
            try:
                payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
            except Exception:
                payload = {}
            # Skip stream.delta for the currently open invoke (already counted above)
            if t == 'stream.delta' and open_invoke and inv == open_invoke:
                continue
            if t == 'stream.delta':
                extra += _count_words_from_payload(payload)
            elif t == 'msg.create':
                # Count user/tool messages; skip assistant content to avoid doubling deltas
                role = payload.get('role')
                if role in ('user', 'tool'):
                    content = payload.get('content')
                    if isinstance(content, str) and content:
                        extra += len(content.split())
                    # For tool messages we only count content above
                elif role == 'assistant':
                    # Only count reasoning text if present (assistant reasoning may not be fully streamed)
                    reas = payload.get('reasoning')
                    if isinstance(reas, str) and reas:
                        extra += len(reas.split())
                # else: ignore other roles
            # ignore other event types
    except Exception:
        pass

    return base + extra

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


async def periodic_reporter(db: ThreadsDB, root_id: str, interval_sec: float = 2.0) -> None:
    """Every interval_sec, print summary of active threads and word counts per thread."""
    import time
    next_report_time = time.time()
    
    while True:
        try:
            # Calculate time until next report
            current_time = time.time()
            sleep_time = max(0.1, next_report_time - current_time)
            
            # Sleep until next report time
            await asyncio.sleep(sleep_time)
            
            # Create snapshots for all children threads before reporting
            subtree = collect_subtree(db, root_id)
            children = [t for t in subtree if t != root_id]
            active = list_active_threads(db, children)
            
            # Create snapshots for all children threads
            for tid in children:
                try:
                    create_snapshot(db, tid)
                except Exception as e:
                    print(f"[status] failed to snapshot {tid[-8:]}: {e}")
            
            # Now do the report
            counts = {tid: word_count_from_events(db, tid) for tid in children}
            active_summary = ", ".join(f"{tid[-8:]}({counts.get(tid,0)})" for tid in active) or "-"
            total_words = sum(counts.values())
            print(f"[status] active {len(active)}/{len(children)} | total_words={total_words} | active: {active_summary}")
            
            # Schedule next report
            next_report_time += interval_sec
        except Exception as e:
            print(f"[status] reporter error: {e}")
            # Reset timing on error
            next_report_time = time.time() + interval_sec


async def wait_subtree_idle(db: ThreadsDB, root_id: str, poll_sec: float = 0.1, quiet_checks: int = 3) -> None:
    # Wait until no runnable threads remain in the subtree for a number of consecutive checks
    subtree = collect_subtree(db, root_id)
    stable = 0
    while True:
        any_run = False
        for tid in subtree:
            if is_thread_runnable(db, tid):
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

    # Create root and 10 children with tasks
    root_id = create_root_thread(db, name="Batch Root")
    num_tasks=20
    tasks = [f"Write a story named story_#{i} into a file story_#{i}.md ." for i in range(1, num_tasks)]

    for i, task in enumerate(tasks, start=1):
        child = create_child_thread(db, root_id, name=f"agent-{i:03d}")#, initial_model_key = "openrouter:openai 120B")
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

    # Start a scheduler for the entire subtree rooted at 'root_id'
    max_concurrent = int(os.environ.get("MAX_CONCURRENT", "8") or "8")
    from eggthreads.runner import RunnerConfig  # type: ignore
    cfg = RunnerConfig(max_concurrent_threads=max_concurrent)
    scheduler = SubtreeScheduler(db, root_thread_id=root_id, config=cfg, models_path=models_path, all_models_path=all_models_path)

    sched_task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    report_task = asyncio.create_task(periodic_reporter(db, root_id, 2.0))
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
