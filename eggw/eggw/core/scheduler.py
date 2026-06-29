"""Scheduler management for eggw backend."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from eggthreads import SubtreeScheduler, get_parent
from eggthreads.runner import scheduler_task_is_live, scheduler_task_status

from . import state

# Import mock LLM utilities
from ..mock_llm import is_test_mode, get_llm_client


def get_thread_root_id(thread_id: str) -> str:
    """Return the root thread id for any thread id."""
    if not state.db:
        return thread_id
    current = thread_id
    while True:
        parent = get_parent(state.db, current)
        if not parent:
            return current
        current = parent


def start_scheduler(root_tid: str) -> None:
    """Start a scheduler for a root thread if not already running."""
    existing = state.active_schedulers.get(root_tid)
    if existing is not None and scheduler_task_is_live(existing.get("task")):
        return  # This process already has a live scheduler for this root.
    if existing is not None:
        status = scheduler_task_status(existing.get("task"))
        state.active_schedulers.pop(root_tid, None)
        print(f"Restarting scheduler for root {root_tid[-8:]} (previous task: {status})")

    if not state.db:
        return

    # Faster scheduler polling for quicker response to user messages
    poll_sec = float(os.environ.get("EGG_POLL_SEC", "0.05"))

    # Use mock LLM in test mode
    llm = None
    if is_test_mode():
        llm = get_llm_client(str(state.MODELS_PATH), str(state.ALL_MODELS_PATH))
        print(f"Using MockLLMClient for scheduler (test mode)")

    sched = SubtreeScheduler(
        state.db,
        root_thread_id=root_tid,
        llm=llm,  # Pass mock LLM if in test mode, None otherwise (scheduler creates its own)
        models_path=str(state.MODELS_PATH),
        all_models_path=str(state.ALL_MODELS_PATH),
        image_generation_models_path=str(state.IMAGE_GENERATION_MODELS_PATH),
    )
    task = asyncio.create_task(sched.run_forever(poll_sec=poll_sec))
    state.active_schedulers[root_tid] = {"scheduler": sched, "task": task}
    add_done_callback = getattr(task, "add_done_callback", None)
    if callable(add_done_callback):
        add_done_callback(lambda done_task, rid=root_tid: _scheduler_task_done(rid, done_task))
    print(f"Started scheduler for root {root_tid[-8:]}")


def _scheduler_task_done(root_tid: str, task: asyncio.Task) -> None:
    """Forget dead scheduler tasks so a later visit restarts them."""

    entry = state.active_schedulers.get(root_tid)
    if entry is not None and entry.get("task") is task:
        state.active_schedulers.pop(root_tid, None)
    status = scheduler_task_status(task)
    if status != "cancelled":
        print(f"Scheduler for root {root_tid[-8:]} stopped ({status})")


def stop_scheduler(root_tid: str) -> None:
    """Stop a scheduler for a root thread."""
    if root_tid not in state.active_schedulers:
        return

    entry = state.active_schedulers.pop(root_tid)
    task = entry.get("task")

    if task and not task.done():
        task.cancel()

    print(f"Stopped scheduler for root {root_tid[-8:]}")


def scheduler_running(root_tid: str) -> bool:
    """Return True only for a live scheduler task in this EggW process."""

    entry = state.active_schedulers.get(root_tid)
    if not isinstance(entry, dict):
        return False
    return scheduler_task_is_live(entry.get("task"))


def ensure_scheduler_for(thread_id: str) -> None:
    """Ensure a scheduler is running for the thread's root."""
    root_id = get_thread_root_id(thread_id)
    if not scheduler_running(root_id):
        start_scheduler(root_id)
