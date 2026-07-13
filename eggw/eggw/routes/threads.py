"""Thread API routes for eggw backend."""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from eggthreads import (
    create_root_thread,
    create_child_thread,
    delete_thread,
    list_threads,
    list_children_ids,
    list_children_with_meta,
    get_parent,
    duplicate_thread,
    current_thread_model,
    thread_state,
    get_active_get_user_message_waiting_note,
    ThreadEventFeed,
    ThreadsDB,
)

from ..models import ThreadInfo, CreateThreadRequest
from .. import core
from ..core import ensure_scheduler_for, get_thread_root_id
from ..core.scheduler import scheduler_running
from ..system_prompt import append_root_system_prompt

router = APIRouter(prefix="/api/threads", tags=["threads"])


def _is_visible_root_thread(thread, parent_set: set[str]) -> bool:
    """Return whether a thread should appear as an EggW top-level chat root."""

    return thread.thread_id not in parent_set


def _thread_created_at(thread) -> str:
    return thread.created_at or ""


def _latest_event_seq(thread_id: str) -> int:
    if not core.db:
        return -1
    try:
        row = core.db.conn.execute(
            "SELECT event_seq FROM events WHERE thread_id=? ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else -1
    except Exception:
        return -1


@router.get("", response_model=List[ThreadInfo])
async def get_threads():
    """List all threads (optimized with bulk queries)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    all_threads = list_threads(core.db)
    if not all_threads:
        return []

    # Bulk fetch parent-child relationships
    children_set: set[str] = set()  # threads that have children
    parent_map: Dict[str, str] = {}  # child_id -> parent_id
    try:
        cur = core.db.conn.execute("SELECT parent_id, child_id FROM children")
        for row in cur.fetchall():
            children_set.add(row[0])  # parent has children
            parent_map[row[1]] = row[0]  # child -> parent
    except Exception:
        pass

    threads = []
    for t in all_threads:
        threads.append(ThreadInfo(
            id=t.thread_id,
            name=t.name,
            parent_id=parent_map.get(t.thread_id),
            model_key=current_thread_model(core.db, t.thread_id),
            created_at=t.created_at,
            has_children=t.thread_id in children_set,
        ))
    return threads


@router.get("/roots", response_model=List[ThreadInfo])
async def get_root_threads():
    """List only root threads (optimized with bulk queries)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    all_threads = list_threads(core.db)
    if not all_threads:
        return []

    # Bulk fetch parent-child relationships
    children_set: set[str] = set()  # threads that have children
    parent_set: set[str] = set()  # threads that have a parent
    try:
        cur = core.db.conn.execute("SELECT parent_id, child_id FROM children")
        for row in cur.fetchall():
            children_set.add(row[0])
            parent_set.add(row[1])
    except Exception:
        pass

    threads = []
    visible_roots = [t for t in all_threads if _is_visible_root_thread(t, parent_set)]
    visible_roots.sort(key=lambda t: (_latest_event_seq(t.thread_id), _thread_created_at(t), t.thread_id))
    for t in visible_roots:
        threads.append(ThreadInfo(
            id=t.thread_id,
            name=t.name,
            parent_id=None,
            model_key=current_thread_model(core.db, t.thread_id),
            created_at=t.created_at,
            has_children=t.thread_id in children_set,
        ))
    return threads


@router.get("/{thread_id}", response_model=ThreadInfo)
async def get_thread(thread_id: str):
    """Get a specific thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    children = list_children_ids(core.db, t.thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(core.db, t.thread_id),
        model_key=current_thread_model(core.db, t.thread_id),
        created_at=t.created_at,
        has_children=len(children) > 0,
    )


@router.post("", response_model=ThreadInfo)
async def create_thread(request: CreateThreadRequest):
    """Create a new thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    chat_keys = core.chat_model_keys(core.models_config, core.llm_client)
    model_key = request.model_key or core.default_model_key or (chat_keys[0] if chat_keys else None)
    if model_key and not core.is_chat_model_key(model_key, core.models_config.get(model_key) or {}, core.llm_client):
        raise HTTPException(status_code=400, detail="Model is not usable for normal chat")

    models_path = str(core.MODELS_PATH)
    all_models_path = str(core.ALL_MODELS_PATH)

    if request.parent_id:
        if not core.db.get_thread(request.parent_id):
            raise HTTPException(status_code=404, detail="Parent thread not found")

        # Create child thread
        thread_id = create_child_thread(
            core.db,
            parent_id=request.parent_id,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
            all_models_path=all_models_path,
        )
    else:
        # Create root thread
        thread_id = create_root_thread(
            core.db,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
            all_models_path=all_models_path,
        )
        append_root_system_prompt(core.db, thread_id)

    ensure_scheduler_for(thread_id)

    t = core.db.get_thread(thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(core.db, t.thread_id),
        model_key=current_thread_model(core.db, t.thread_id),
        created_at=t.created_at,
        has_children=False,
    )


@router.patch("/{thread_id}")
async def update_thread(thread_id: str, name: Optional[str] = None):
    """Update thread properties (e.g., name)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    if name is not None:
        core.db.conn.execute(
            "UPDATE threads SET name = ? WHERE thread_id = ?",
            (name, thread_id)
        )
        core.db.conn.commit()

    t = core.db.get_thread(thread_id)
    children = list_children_ids(core.db, t.thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(core.db, t.thread_id),
        model_key=current_thread_model(core.db, t.thread_id),
        created_at=t.created_at,
        has_children=len(children) > 0,
    )


@router.delete("/{thread_id}")
async def remove_thread(thread_id: str, delete_subtree: bool = False):
    """Delete a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    delete_thread(core.db, thread_id, delete_subtree=delete_subtree)
    return {"status": "deleted"}


@router.post("/{thread_id}/duplicate", response_model=ThreadInfo)
async def duplicate_thread_endpoint(thread_id: str, name: Optional[str] = None):
    """Duplicate a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    new_id = duplicate_thread(core.db, thread_id, name=name)
    t = core.db.get_thread(new_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(core.db, t.thread_id),
        model_key=current_thread_model(core.db, t.thread_id),
        created_at=t.created_at,
        has_children=False,
    )


@router.get("/{thread_id}/children", response_model=List[ThreadInfo])
async def get_thread_children(thread_id: str):
    """Get children of a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    children = []
    # list_children_with_meta returns tuples: (child_id, name, short_recap, created_at)
    for child_id, name, short_recap, created_at in list_children_with_meta(core.db, thread_id):
        grandchildren = list_children_ids(core.db, child_id)
        children.append(ThreadInfo(
            id=child_id,
            name=name,
            parent_id=thread_id,
            model_key=current_thread_model(core.db, child_id),
            created_at=created_at,
            has_children=len(grandchildren) > 0,
        ))
    return children


@router.get("/{thread_id}/state")
async def get_thread_state_endpoint(
    thread_id: str,
    snapshot_cursor: int | None = Query(default=None, ge=-1),
):
    """Get the current state of a thread (running, waiting, etc.)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    get_user_waiting_note = get_active_get_user_message_waiting_note(core.db, thread_id)
    state = "waiting_user" if get_user_waiting_note is not None else thread_state(core.db, thread_id)
    root_id = get_thread_root_id(thread_id)

    # This is the explicit initial live replay contract shared with the SSE
    # feed. The caller supplies its coherent message snapshot cursor; active
    # work rewinds only to the exact live invocation's stream.open. Legacy
    # state callers that omit it get the current idle cursor.
    if snapshot_cursor is None:
        try:
            snapshot_cursor = int(core.db.max_event_seq(thread_id))
        except Exception:
            snapshot_cursor = -1
    replay_db = ThreadsDB(core.db.path)
    try:
        replay = ThreadEventFeed(replay_db).replay_cursor(thread_id, snapshot_cursor)
    finally:
        replay_db.conn.close()
    streaming_kind = replay.streaming_kind
    streaming_invoke_id = replay.active_invoke_id

    return {
        "state": state,
        "streaming_kind": streaming_kind,
        "streaming_invoke_id": streaming_invoke_id,
        "live_replay_cursor": replay.after_seq,
        "active_get_user_wait": get_user_waiting_note is not None,
        "get_user_waiting_note": get_user_waiting_note,
        "scheduler_running": scheduler_running(root_id),
    }
