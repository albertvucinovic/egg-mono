"""Compaction slash commands for eggw."""
from __future__ import annotations

from eggthreads import (
    append_compaction_summary_request,
    commit_thread_compaction,
    create_snapshot,
    set_thread_compaction_context_length,
)
from eggthreads.builtin_plugins.compaction import build_context_status

from .. import core
from ..core import ensure_scheduler_for
from ..models import CommandResponse


async def cmd_compact(thread_id: str, selector: str) -> CommandResponse:
    """Set provider/API context start for the current thread."""

    if not core.db:
        return CommandResponse(success=False, message="Database not initialized")

    result = commit_thread_compaction(
        core.db,
        thread_id,
        (selector or "").strip() or None,
        created_by="user_command",
    )
    if result.success:
        create_snapshot(core.db, thread_id)
    return CommandResponse(
        success=bool(result.success),
        message=result.message if result.success else f"/compact: {result.message}",
        data={
            "selector": result.selector,
            "start_msg_id": result.start_msg_id,
            "start_event_seq": result.start_event_seq,
            "compaction_event_seq": result.compaction_event_seq,
        },
    )


async def cmd_compact_with_summary(thread_id: str) -> CommandResponse:
    """Queue a model-visible summary request and start the scheduler."""

    if not core.db:
        return CommandResponse(success=False, message="Database not initialized")

    try:
        request_msg_id = append_compaction_summary_request(
            core.db,
            thread_id,
            created_by="user_command",
        )
        create_snapshot(core.db, thread_id)
        ensure_scheduler_for(thread_id)
    except Exception as e:
        return CommandResponse(success=False, message=f"/compactWithSummary failed: {e}")

    return CommandResponse(
        success=True,
        message=(
            "Queued compaction summary request; the assistant will write a "
            "normal summary and then call compact_thread()."
        ),
        data={"request_msg_id": request_msg_id},
    )


async def cmd_set_auto_compact_threshold(thread_id: str, arg: str) -> CommandResponse:
    """Set the thread-level automatic compaction threshold."""

    if not core.db:
        return CommandResponse(success=False, message="Database not initialized")

    text = (arg or "").strip()
    if not text:
        return CommandResponse(
            success=False,
            message="Usage: /setAutoCompactThreshold <tokens> (0 disables auto-compaction)",
        )
    try:
        threshold = int(text)
    except ValueError:
        return CommandResponse(
            success=False,
            message=f"Invalid number: {text}. Usage: /setAutoCompactThreshold <tokens>",
        )

    event_seq = set_thread_compaction_context_length(
        core.db,
        thread_id,
        threshold,
        created_by="user_command",
    )
    if threshold <= 0:
        message = f"Auto-compaction disabled for thread {thread_id[-8:]} (event #{event_seq})."
    else:
        message = f"Auto-compaction threshold set to {threshold:,} tokens for thread {thread_id[-8:]} (event #{event_seq})."
    return CommandResponse(
        success=True,
        message=message,
        data={"threshold_tokens": threshold, "event_seq": event_seq},
    )


async def cmd_context(thread_id: str) -> CommandResponse:
    """Show current context, compaction, and auto-compaction status."""

    if not core.db:
        return CommandResponse(success=False, message="Database not initialized")

    message, data = build_context_status(core.db, thread_id, llm=core.llm_client)

    return CommandResponse(
        success=True,
        message=message,
        data=data,
    )
