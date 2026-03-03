"""Scheduler management for eggw backend."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from eggthreads import SubtreeScheduler, get_parent

from . import state
from .state import (
    MODELS_PATH,
    ALL_MODELS_PATH,
)

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
    if root_tid in state.active_schedulers:
        return  # Already running

    if not state.db:
        return

    # Faster scheduler polling for quicker response to user messages
    poll_sec = float(os.environ.get("EGG_POLL_SEC", "0.05"))

    # Use mock LLM in test mode
    llm = None
    if is_test_mode():
        llm = get_llm_client(str(MODELS_PATH), str(ALL_MODELS_PATH))
        print(f"Using MockLLMClient for scheduler (test mode)")

    sched = SubtreeScheduler(
        state.db,
        root_thread_id=root_tid,
        llm=llm,  # Pass mock LLM if in test mode, None otherwise (scheduler creates its own)
        models_path=str(MODELS_PATH),
        all_models_path=str(ALL_MODELS_PATH),
    )
    task = asyncio.create_task(sched.run_forever(poll_sec=poll_sec))
    state.active_schedulers[root_tid] = {"scheduler": sched, "task": task}
    print(f"Started scheduler for root {root_tid[-8:]}")


def stop_scheduler(root_tid: str) -> None:
    """Stop a scheduler for a root thread."""
    if root_tid not in state.active_schedulers:
        return

    entry = state.active_schedulers.pop(root_tid)
    task = entry.get("task")

    if task and not task.done():
        task.cancel()

    print(f"Stopped scheduler for root {root_tid[-8:]}")


def ensure_scheduler_for(thread_id: str) -> None:
    """Ensure a scheduler is running for the thread's root."""
    root_id = get_thread_root_id(thread_id)
    if root_id not in state.active_schedulers:
        start_scheduler(root_id)
