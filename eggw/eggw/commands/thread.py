"""Thread management commands for eggw backend."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from eggthreads import (
    create_root_thread,
    create_child_thread,
    append_message,
    delete_thread,
    list_threads,
    list_children_with_meta,
    get_parent,
    current_thread_model,
    duplicate_thread,
    duplicate_thread_up_to,
    continue_thread,
    is_thread_continuable,
    interrupt_thread,
    parse_args,
    get_thread_statuses_bulk,
)

from ..models import CommandResponse
from .. import core
from ..core import (
    MODELS_PATH,
    get_thread_root_id,
    ensure_scheduler_for,
)


async def cmd_spawn(thread_id: str, context: str) -> CommandResponse:
    """Handle /spawn or /spawnChildThread command."""
    models_path = str(MODELS_PATH)

    # Get parent's model
    parent_model = current_thread_model(core.db, thread_id)

    # Create child thread
    child_id = create_child_thread(
        core.db,
        parent_id=thread_id,
        initial_model_key=parent_model,
        models_path=models_path,
    )

    # If context provided, add it as a user message
    if context.strip():
        append_message(core.db, child_id, 'user', context.strip())
        ensure_scheduler_for(child_id)

    return CommandResponse(
        success=True,
        message=f"Spawned child thread: {child_id[-8:]}",
        data={"child_id": child_id, "parent_id": thread_id},
    )


async def cmd_new_thread(name: str) -> CommandResponse:
    """Handle /newThread command."""
    models_path = str(MODELS_PATH)
    model_key = core.default_model_key or next(iter(core.models_config.keys()), None)

    thread_id = create_root_thread(
        core.db,
        name=name if name else None,
        initial_model_key=model_key,
        models_path=models_path,
    )

    return CommandResponse(
        success=True,
        message=f"Created new thread: {thread_id[-8:]}",
        data={"thread_id": thread_id},
    )


async def cmd_parent_thread(thread_id: str) -> CommandResponse:
    """Handle /parentThread command."""
    parent_id = get_parent(core.db, thread_id)
    if not parent_id:
        return CommandResponse(
            success=False,
            message="This thread has no parent (it's a root thread)",
        )
    return CommandResponse(
        success=True,
        message=f"Parent thread: {parent_id[-8:]}",
        data={"thread_id": parent_id},
    )


async def cmd_switch_thread(selector: str) -> CommandResponse:
    """Handle /thread command to switch to a thread by ID, partial ID, name, or recap."""
    if not selector:
        return CommandResponse(success=False, message="Usage: /thread <id or partial-id or name or recap>")

    # Try exact match first
    t = core.db.get_thread(selector)
    if t:
        return CommandResponse(
            success=True,
            message=f"Switched to thread: {selector[-8:]}",
            data={"thread_id": selector},
        )

    # Try partial match on id, name, and recap (case-insensitive)
    all_threads = list_threads(core.db)
    sel_lower = selector.lower()
    matches = []
    for t in all_threads:
        # Build searchable string from id, name, and recap
        hay = f"{t.thread_id} {t.name or ''} {t.short_recap or ''}".lower()
        if sel_lower in hay:
            matches.append(t)

    if len(matches) == 1:
        tid = matches[0].thread_id
        name_part = f" ({matches[0].name})" if matches[0].name else ""
        return CommandResponse(
            success=True,
            message=f"Switched to thread: {tid[-8:]}{name_part}",
            data={"thread_id": tid},
        )
    elif len(matches) > 1:
        match_list = ", ".join(
            f"{t.thread_id[-8:]}" + (f" ({t.name})" if t.name else "")
            for t in matches[:5]
        )
        return CommandResponse(
            success=False,
            message=f"Ambiguous thread selector ({len(matches)} matches): {match_list}",
        )
    else:
        return CommandResponse(success=False, message=f"No thread found matching: {selector}")


async def cmd_list_threads() -> CommandResponse:
    """Handle /threads command - shows thread tree structure (optimized)."""
    # Fetch all data in bulk to avoid N+1 queries
    all_threads = list_threads(core.db)
    if not all_threads:
        return CommandResponse(success=True, message="No threads found")

    # Build lookup maps
    threads_by_id = {t.thread_id: t for t in all_threads}

    # Fetch all parent-child relationships in one query
    children_map: Dict[str, List[str]] = {}  # parent_id -> [child_ids]
    parent_map: Dict[str, str] = {}  # child_id -> parent_id
    try:
        cur = core.db.conn.execute("SELECT parent_id, child_id FROM children")
        for row in cur.fetchall():
            parent_id, child_id = row[0], row[1]
            if parent_id not in children_map:
                children_map[parent_id] = []
            children_map[parent_id].append(child_id)
            parent_map[child_id] = parent_id
    except Exception:
        pass

    # Find roots (threads with no parent)
    roots = [t.thread_id for t in all_threads if t.thread_id not in parent_map]

    # Fetch all model settings in one query
    model_map: Dict[str, str] = {}  # thread_id -> model_key
    try:
        cur = core.db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
        for row in cur.fetchall():
            model_map[row[0]] = row[1]
    except Exception:
        pass

    # For threads without explicit model, use initial_model_key
    for t in all_threads:
        if t.thread_id not in model_map and t.initial_model_key:
            model_map[t.thread_id] = t.initial_model_key

    # Pre-compute real-time status for all threads in one batch (efficient)
    all_tids = [t.thread_id for t in all_threads]
    status_map = get_thread_statuses_bulk(core.db, all_tids)

    def format_thread(tid: str, indent: int = 0, max_depth: int = 50) -> list[str]:
        """Format a thread and its children recursively."""
        if indent > max_depth:
            return ["  " * indent + "... (max depth reached)"]

        lines = []
        t = threads_by_id.get(tid)
        if not t:
            return lines

        # Build thread line
        prefix = "  " * indent + ("├─ " if indent > 0 else "")
        name_part = f" ({t.name})" if t.name else ""
        model = model_map.get(tid, "")
        model_part = f" [{model}]" if model else ""
        # Use real-time status instead of stale database status
        state = status_map.get(tid, "idle")
        state_part = f" <{state}>" if state != "idle" else ""

        lines.append(f"{prefix}{tid[-8:]}{name_part}{model_part}{state_part}")

        # Get children and recurse
        children = children_map.get(tid, [])
        for child_id in children:
            lines.extend(format_thread(child_id, indent + 1, max_depth))

        return lines

    lines = []
    for root_id in roots:
        lines.extend(format_thread(root_id))

    total = len(all_threads)
    return CommandResponse(
        success=True,
        message=f"Threads ({total} total, {len(roots)} roots):\n" + "\n".join(lines),
        data={"threads": roots, "total": total},
    )


async def cmd_list_children(thread_id: str) -> CommandResponse:
    """Handle /listChildren command."""
    children = list_children_with_meta(core.db, thread_id)
    if not children:
        return CommandResponse(success=True, message="No children")

    lines = []
    for child_id, name, recap, created in children:
        name_part = f" ({name})" if name else ""
        lines.append(f"  {child_id[-8:]}{name_part}")

    return CommandResponse(
        success=True,
        message=f"Children ({len(children)}):\n" + "\n".join(lines),
        data={"children": [c[0] for c in children]},
    )


async def cmd_delete_thread(current_thread_id: str, selector: str) -> CommandResponse:
    """Handle /deleteThread command."""
    target_id = selector.strip() if selector else current_thread_id

    # Try to find the thread
    t = core.db.get_thread(target_id)
    if not t:
        # Try partial match
        all_threads = list_threads(core.db)
        matches = [th for th in all_threads if target_id.lower() in th.thread_id.lower()]
        if len(matches) == 1:
            target_id = matches[0].thread_id
        elif len(matches) > 1:
            return CommandResponse(success=False, message="Ambiguous thread selector")
        else:
            return CommandResponse(success=False, message="Thread not found")

    delete_thread(core.db, target_id, delete_subtree=True)
    return CommandResponse(
        success=True,
        message=f"Deleted thread: {target_id[-8:]}",
        data={"deleted_id": target_id},
    )


async def cmd_duplicate_thread(thread_id: str, command_arg: str) -> CommandResponse:
    """Handle /duplicateThread command.

    Usage:
        /duplicateThread                           - duplicate with default name
        /duplicateThread <name>                    - duplicate with custom name
        /duplicateThread <name> <msg_id>           - duplicate up to msg_id
        /duplicateThread name=<name> msg_id=<id>   - named arguments
        /duplicateThread <threadId> <name> <msg_id> - duplicate another thread
    """
    args = parse_args(command_arg)

    # Parse arguments - support multiple formats
    source_thread_id = thread_id
    name = None
    up_to_msg_id = None

    # Check for named arguments first
    if args.named:
        name = args.named.get('name')
        up_to_msg_id = args.named.get('msg_id')
        if 'thread_id' in args.named or 'threadId' in args.named:
            source_thread_id = args.named.get('thread_id') or args.named.get('threadId')

    # Parse positional arguments
    if args.positional:
        if len(args.positional) == 1:
            name = args.positional[0]
        elif len(args.positional) == 2:
            name = args.positional[0]
            up_to_msg_id = args.positional[1]
        elif len(args.positional) >= 3:
            source_thread_id = args.positional[0]
            name = args.positional[1]
            up_to_msg_id = args.positional[2]

    try:
        if up_to_msg_id:
            new_id = duplicate_thread_up_to(core.db, source_thread_id, up_to_msg_id, name=name)
        else:
            new_id = duplicate_thread(core.db, source_thread_id, name=name)
    except ValueError as e:
        return CommandResponse(success=False, message=str(e))

    return CommandResponse(
        success=True,
        message=f"Duplicated to: {new_id[-8:]}",
        data={"thread_id": new_id, "source_id": source_thread_id, "reload": True},
    )


async def cmd_continue(thread_id: str, command_arg: str) -> CommandResponse:
    """Handle /continue command.

    Usage:
        /continue                    - auto-detect continue point
        /continue <msg_id>           - continue from specific message
        /continue wait=30            - delay 30s before applying continue (e.g., for API rate limits)
        /continue msg_id=<id> wait=<sec>  - named arguments
    """
    args = parse_args(command_arg)

    # Extract arguments
    msg_id = args.named.get('msg_id') or args.positional_or(0)
    delay_sec = args.get_float('wait')

    # Auto-interrupt if thread is streaming
    root_id = get_thread_root_id(thread_id) if thread_id else None
    was_interrupted = False
    if root_id and root_id in core.active_schedulers:
        # Thread is streaming - interrupt first
        interrupt_thread(core.db, thread_id, reason="continue")
        was_interrupted = True
        # Brief delay to let interrupt propagate
        await asyncio.sleep(0.1)

    # Check if thread is continuable (should be now after interrupt)
    if not is_thread_continuable(core.db, thread_id):
        return CommandResponse(
            success=False,
            message="Thread cannot be continued (may be waiting for input)"
        )

    # If delay requested, schedule the continue for later
    if delay_sec is not None and delay_sec > 0:
        async def delayed_continue():
            await asyncio.sleep(delay_sec)
            continue_thread(core.db, thread_id, msg_id=msg_id)

        asyncio.create_task(delayed_continue())
        return CommandResponse(
            success=True,
            message=f"Continue scheduled in {delay_sec}s" + (f" from message {msg_id[-8:]}" if msg_id else ""),
            data={
                "delay_sec": delay_sec,
                "msg_id": msg_id,
            }
        )

    # Execute continue immediately
    result = continue_thread(core.db, thread_id, msg_id=msg_id)

    if result.success:
        msg = result.message
        if was_interrupted:
            msg = f"Interrupted streaming. {msg}"
        response_data = {
            "continue_from": result.continue_from_msg_id,
            "skipped_count": len(result.skipped_msg_ids),
            "was_interrupted": was_interrupted,
            "reload": True,  # Signal frontend to refresh messages
        }
        # Include diagnosis details if available
        if result.diagnosis:
            response_data["diagnosis"] = {
                "is_healthy": result.diagnosis.is_healthy,
                "issues": result.diagnosis.issues,
                "details": result.diagnosis.details,
            }
        return CommandResponse(
            success=True,
            message=msg,
            data=response_data
        )
    else:
        return CommandResponse(success=False, message=result.message)


async def cmd_rename(thread_id: str, new_name: str) -> CommandResponse:
    """Handle /rename command."""
    if not new_name:
        t = core.db.get_thread(thread_id)
        current_name = t.name if t and t.name else "(no name)"
        return CommandResponse(
            success=False,
            message=f"Usage: /rename <new name>\nCurrent name: {current_name}",
        )

    core.db.conn.execute(
        "UPDATE threads SET name = ? WHERE thread_id = ?",
        (new_name, thread_id)
    )
    core.db.conn.commit()

    return CommandResponse(
        success=True,
        message=f"Thread renamed to: {new_name}",
        data={"name": new_name},
    )


async def cmd_spawn_auto_approved(thread_id: str, context: str) -> CommandResponse:
    """Handle /spawnAutoApprovedChildThread command."""
    from eggthreads import approve_tool_calls_for_thread

    models_path = str(MODELS_PATH)

    # Get parent's model
    parent_model = current_thread_model(core.db, thread_id)

    # Create child thread
    child_id = create_child_thread(
        core.db,
        parent_id=thread_id,
        initial_model_key=parent_model,
        models_path=models_path,
    )

    # Enable auto-approval for the child
    approve_tool_calls_for_thread(
        core.db, child_id,
        decision='global_approval',
        reason='Spawned with auto-approval enabled'
    )

    # If context provided, add it as a user message
    if context.strip():
        append_message(core.db, child_id, 'user', context.strip())
        ensure_scheduler_for(child_id)

    return CommandResponse(
        success=True,
        message=f"Spawned auto-approved child thread: {child_id[-8:]}",
        data={"child_id": child_id, "parent_id": thread_id, "auto_approved": True},
    )
