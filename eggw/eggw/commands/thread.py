"""Thread management commands for eggw backend."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict

from eggthreads import (
    create_root_thread,
    create_child_thread,
    append_message,
    delete_thread,
    list_threads,
    get_thread_tree,
    resolve_thread_tree_root,
    list_children_with_meta,
    get_parent,
    current_thread_model,
    duplicate_thread,
    duplicate_thread_up_to,
    continue_thread,
    validate_continue_target,
    is_thread_continuable,
    interrupt_thread,
    parse_args,
)
from eggthreads.api import append_continue_recovery_notice

from ..models import CommandResponse
from .. import core
from ..core import ensure_scheduler_for
from ..system_prompt import append_root_system_prompt


def _thread_has_active_lease(thread_id: str) -> bool:
    """Return True only when this exact thread has a live runner lease."""

    try:
        row = core.db.current_open(thread_id)
    except Exception:
        return False
    if not row:
        return False
    try:
        lease_until = row["lease_until"]
    except Exception:
        lease_until = None
    if not lease_until:
        return False
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return str(lease_until) > now_iso


async def cmd_spawn(thread_id: str, context: str) -> CommandResponse:
    """Handle /spawnChildThread command."""
    models_path = str(core.MODELS_PATH)
    all_models_path = str(core.ALL_MODELS_PATH)

    # Get parent's model
    parent_model = current_thread_model(core.db, thread_id)

    # Create child thread
    child_id = create_child_thread(
        core.db,
        parent_id=thread_id,
        initial_model_key=parent_model,
        models_path=models_path,
        all_models_path=all_models_path,
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
    models_path = str(core.MODELS_PATH)
    all_models_path = str(core.ALL_MODELS_PATH)
    chat_keys = core.chat_model_keys(core.models_config, core.llm_client)
    model_key = core.default_model_key or (chat_keys[0] if chat_keys else None)

    thread_id = create_root_thread(
        core.db,
        name=name if name else None,
        initial_model_key=model_key,
        models_path=models_path,
        all_models_path=all_models_path,
    )
    append_root_system_prompt(core.db, thread_id)
    ensure_scheduler_for(thread_id)

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
        ensure_scheduler_for(selector)
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
        ensure_scheduler_for(tid)
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


async def cmd_list_threads(arg: str = "") -> CommandResponse:
    """Handle /threads, optionally rendering one selected subtree."""
    from eggthreads import parse_thread_tree_request

    selector, status_mode = parse_thread_tree_request(arg or "")
    root_id: str | None = None
    if selector:
        try:
            root_id = resolve_thread_tree_root(core.db, selector)
        except ValueError as exc:
            return CommandResponse(success=False, message=str(exc))

    tree = get_thread_tree(
        core.db, root_id, include_runnability=status_mode == "full"
    )
    if not tree:
        return CommandResponse(success=True, message="No threads found")

    nodes_by_id: Dict[str, Dict[str, Any]] = {}

    pending_nodes = list(reversed(tree))
    while pending_nodes:
        node = pending_nodes.pop()
        nodes_by_id[str(node["id"])] = node
        pending_nodes.extend(reversed(node["children"]))

    all_tids = list(nodes_by_id)

    lines: list[str] = []
    pending_render = [(root, 0) for root in reversed(tree)]
    while pending_render:
        node, indent = pending_render.pop()
        if indent > 50:
            lines.append("  " * indent + "... (max depth reached)")
            continue
        tid = str(node["id"])
        prefix = "  " * indent + ("├─ " if indent > 0 else "")
        name = node.get("name")
        name_part = f" ({name})" if name else ""
        model = node.get("model") or ""
        model_part = f" [{model}]" if model else ""
        state = node.get("state") or "idle"
        state_part = f" <{state}>" if state != "idle" else ""
        lines.append(f"{prefix}{tid[-8:]}{name_part}{model_part}{state_part}")
        pending_render.extend(
            (child, indent + 1) for child in reversed(node["children"])
        )

    total = len(all_tids)
    notes: list[str] = []
    if status_mode != "full":
        notes.append(
            "fast status mode: streaming only; use `/threads status=full` to scan runnable state"
        )
    note_part = ""
    if notes:
        note_part = "\n" + "\n".join(f"Note: {note}" for note in notes)
    return CommandResponse(
        success=True,
        message=(
            f"Threads ({total} total, {len(tree)} roots, {len(lines)} shown):"
            f"{note_part}\n" + "\n".join(lines)
        ),
        data={
            "threads": [str(root["id"]) for root in tree],
            "thread_ids": all_tids,
            "total": total,
            "visible_total": len(lines),
            "status_mode": status_mode,
        },
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

    ensure_scheduler_for(new_id)

    return CommandResponse(
        success=True,
        message=f"Duplicated to: {new_id[-8:]}",
        data={"thread_id": new_id, "source_id": source_thread_id, "reload": True},
    )


def parse_continue_command_args(command_arg: str) -> tuple[str | None, float | None]:
    """Parse EggW /continue arguments identically for route and handler."""

    args = parse_args(command_arg)
    return args.named.get("msg_id") or args.positional_or(0), args.get_float("wait")


def validate_continue_command_target(thread_id: str, command_arg: str):
    """Side-effect-free EggW preflight for an explicit /continue target."""

    msg_id, _delay_sec = parse_continue_command_args(command_arg)
    return validate_continue_target(core.db, thread_id, msg_id)


async def cmd_continue(thread_id: str, command_arg: str) -> CommandResponse:
    """Handle /continue command.

    Usage:
        /continue                    - auto-detect continue point
        /continue <msg_id>           - continue from specific message
        /continue wait=30            - delay 30s before applying continue (e.g., for API rate limits)
        /continue msg_id=<id> wait=<sec>  - named arguments
    """
    # Parse through the same helper used by the route-level preflight.
    msg_id, delay_sec = parse_continue_command_args(command_arg)

    # Explicit targets must be proven before any command-layer mutation:
    # interrupting a live lease, scheduling a delayed task, appending recovery
    # notices, or restarting a scheduler. No-argument auto-diagnosis deliberately
    # bypasses this preflight and keeps its existing semantics.
    target_validation = validate_continue_target(core.db, thread_id, msg_id)
    if not target_validation.success:
        return CommandResponse(success=False, message=target_validation.message)

    # Auto-interrupt only if this exact thread is currently streaming.  A
    # running subtree scheduler is not the same thing as a live thread lease:
    # schedulers stay resident while idle, and treating that as "streaming"
    # can cancel a pending RA1 turn by appending a purpose='llm' interrupt.
    was_interrupted = False
    if _thread_has_active_lease(thread_id):
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
            result = continue_thread(core.db, thread_id, msg_id=msg_id)
            if result.success:
                append_continue_recovery_notice(core.db, thread_id, result)
                ensure_scheduler_for(thread_id)

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
        append_continue_recovery_notice(core.db, thread_id, result)
        ensure_scheduler_for(thread_id)
        msg = result.message
        if was_interrupted:
            msg = f"Interrupted streaming. {msg}"
        response_data = {
            "continue_from": result.continue_from_msg_id,
            "skipped_count": len(result.skipped_msg_ids),
            "was_interrupted": was_interrupted,
            # Continuation invalidates the loaded page chain; unlike ordinary
            # reload commands, the browser must rewind generation authority
            # before fetching the disjoint post-continue tail.
            "reload": True,
            "reload_mode": "continuation",
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

    models_path = str(core.MODELS_PATH)
    all_models_path = str(core.ALL_MODELS_PATH)

    # Get parent's model
    parent_model = current_thread_model(core.db, thread_id)

    # Create child thread
    child_id = create_child_thread(
        core.db,
        parent_id=thread_id,
        initial_model_key=parent_model,
        models_path=models_path,
        all_models_path=all_models_path,
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
