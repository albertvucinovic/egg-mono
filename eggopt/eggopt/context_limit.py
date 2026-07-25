from __future__ import annotations

import asyncio
from typing import Any

from eggflow import ContextLimitExceededError
from eggthreads import ThreadsDB, interrupt_thread, thread_token_stats


def full_context_tokens(db: ThreadsDB, thread_id: str) -> int:
    """Return Eggthreads' full effective history, not its provider prompt."""

    stats = thread_token_stats(db, thread_id)
    return int(stats["full_thread_tokens"])


async def run_with_full_context_limit(
    runner: Any,
    db: ThreadsDB,
    thread_id: str,
    limit: int | None,
    *,
    operation: str,
) -> bool:
    """Run one Eggthreads step while enforcing an Eggopt full-history budget."""

    if limit is None:
        return await runner.run_once()
    current = full_context_tokens(db, thread_id)
    if current >= limit:
        raise ContextLimitExceededError(
            f"{operation} full context limit reached before provider call; "
            f"operation terminated ({current} >= {limit})"
        )
    task = asyncio.create_task(runner.run_once())
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=10.0)
            if task in done:
                break
            current = full_context_tokens(db, thread_id)
            if current >= limit:
                if task.done():
                    break
                interrupt_thread(
                    db,
                    thread_id,
                    reason=f"{operation} full context limit reached: {current} >= {limit}",
                )
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise ContextLimitExceededError(
                    f"{operation} full context limit reached; operation terminated"
                )
        return bool(await task)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = ["full_context_tokens", "run_with_full_context_limit"]
