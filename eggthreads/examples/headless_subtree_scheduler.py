#\!/usr/bin/env python3
"""
Headless example: drive an entire subtree by running a single SubtreeScheduler on the root.
The scheduler starts a one-invoke-per-turn ThreadRunner for every runnable thread in the subtree,
continuing automatically after tool calls (new invoke_id per turn).

Run:
  python3 -u examples/headless_subtree_scheduler.py

Optional environment variables:
  SYSTEM_PROMPT_PATH      Path to system prompt file (default: systemPrompt or builtin)
  EGG_MODELS_PATH         Path to models.json (default: models.json)
  EGG_ALL_MODELS_PATH     Path to all-models.json (default: all-models.json)
  MAX_CONCURRENT          Concurrency limit for scheduler (default: 8)
"""
import os
import sys
import asyncio
from pathlib import Path

# Allow running this file directly: add project root to sys.path
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except Exception:
    pass


from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    RunnerConfig,
    create_llm_client,
    create_root_thread,
    create_child_thread,
    append_message,
    create_snapshot,
    set_subtree_tools_enabled,
    collect_subtree,
    wait_subtree_idle,
)
from examples.reporting import periodic_reporter

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
        child = create_child_thread(db, root_id, name=f"agent-{i:03d}")
        append_message(db, child, "system", system_prompt)
        append_message(db, child, "user", task)
        create_snapshot(db, child)

    # Allow threads in this subtree to use tools for this batch run.
    try:
        set_subtree_tools_enabled(db, root_id, True)
    except Exception as e:
        print(f"[status] warning: failed to enable tools for subtree: {e}")

    # Also auto-approve tool calls *for the first user turn*
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

    max_concurrent = int(os.environ.get("MAX_CONCURRENT", "8") or "8")
    cfg = RunnerConfig(max_concurrent_threads=max_concurrent)
    llm_client = create_llm_client(models_path=models_path, all_models_path=all_models_path)
    scheduler = SubtreeScheduler(
        db,
        root_thread_id=root_id,
        llm=llm_client,
        config=cfg,
        models_path=models_path,
        all_models_path=all_models_path,
    )

    sched_task = asyncio.create_task(scheduler.run_forever(poll_sec=0.05))
    report_task = asyncio.create_task(periodic_reporter(db, root_id, 2.0, llm=llm_client))
    print("Created tasks\!")

    # Wait until subtree becomes idle
    await wait_subtree_idle(db, root_id)

    # Cancel tasks
    sched_task.cancel()
    report_task.cancel()
    await asyncio.sleep(0)

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
