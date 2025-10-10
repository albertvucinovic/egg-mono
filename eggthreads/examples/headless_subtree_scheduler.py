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
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
except Exception:
    pass

from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    create_root_thread,
    create_child_thread,
    append_message,
    create_snapshot,
)

SYSTEM_PROMPT_DEFAULT = "You are a helpful assistant."


def _env_path(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v and isinstance(v, str) and v.strip():
        return v.strip()
    # Try relative to project root
    project_root = Path(__file__).resolve().parents[2]
    if name == "EGG_MODELS_PATH":
        cand = project_root / "egg" / "models.json"
        if cand.exists():
            return str(cand)
        cand = project_root / "models.json"
        if cand.exists():
            return str(cand)
    elif name == "EGG_ALL_MODELS_PATH":
        cand = project_root / "egg" / "all-models.json"
        if cand.exists():
            return str(cand)
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
    for cand in ("egg/systemPrompt", "systemPrompt"):
        if os.path.exists(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
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


def is_thread_runnable(db: ThreadsDB, tid: str) -> bool:
    # A thread is runnable if there is a msg.create (user/tool/assistant with tool_calls)
    # strictly after the last stream.close.
    row_close = db.conn.execute(
        "SELECT MAX(event_seq) FROM events WHERE thread_id=? AND type='stream.close'",
        (tid,)
    ).fetchone()
    last_close_seq = int(row_close[0]) if row_close and row_close[0] is not None else -1
    row = db.conn.execute(
        """
        SELECT 1 FROM events e
         WHERE e.thread_id=?
           AND e.event_seq>?
           AND e.type='msg.create'
           AND (
                json_extract(e.payload_json,'$.role') IN ('user','tool')
             OR (
                  json_extract(e.payload_json,'$.role')='assistant'
              AND json_extract(e.payload_json,'$.tool_calls') IS NOT NULL
                )
           )
         LIMIT 1
        """,
        (tid, last_close_seq)
    ).fetchone()
    return bool(row)


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
                    print(f"[status] failed to snapshot {tid[:8]}: {e}")
            
            # Now do the report
            counts = {tid: word_count_from_snapshot(db, tid) for tid in children}
            active_summary = ", ".join(f"{tid[:8]}({counts.get(tid,0)})" for tid in active) or "-"
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
    models_path = _env_path("EGG_MODELS_PATH", "egg/models.json")
    all_models_path = _env_path("EGG_ALL_MODELS_PATH", "egg/all-models.json")

    # Create root and 10 children with tasks
    root_id = create_root_thread(db, name="Batch Root")
    tasks = [f"Write a story named story_#{i} into a file story_#{i}.md and include <short_recap>...</short_recap>." for i in range(1, 11)]

    for i, task in enumerate(tasks, start=1):
        child = create_child_thread(db, root_id, name=f"agent-{i:03d}")
        append_message(db, child, "system", system_prompt)
        append_message(db, child, "user", task)
        create_snapshot(db, child)

    # Start a scheduler for the entire subtree rooted at 'root_id'
    max_concurrent = int(os.environ.get("MAX_CONCURRENT", "8") or "8")
    from eggthreads.eggthreads.runner import RunnerConfig  # type: ignore
    cfg = RunnerConfig(max_concurrent_threads=max_concurrent)
    scheduler = SubtreeScheduler(db, root_thread_id=root_id, config=cfg, models_path=models_path, all_models_path=all_models_path)

    sched_task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    report_task = asyncio.create_task(periodic_reporter(db, root_id, 2.0))

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
            print(f"{tid[:8]}: {recap or '(no short_recap found)'}")


if __name__ == "__main__":
    asyncio.run(main())
