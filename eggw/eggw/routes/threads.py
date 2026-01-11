"""Thread API routes for eggw backend."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException

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
)

from models import ThreadInfo, CreateThreadRequest
import core
from core import get_thread_root_id

router = APIRouter(prefix="/api/threads", tags=["threads"])


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

    # Bulk fetch model settings
    model_map: Dict[str, str] = {}
    try:
        cur = core.db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
        for row in cur.fetchall():
            model_map[row[0]] = row[1]
    except Exception:
        pass

    threads = []
    for t in all_threads:
        model = model_map.get(t.thread_id) or t.initial_model_key
        threads.append(ThreadInfo(
            id=t.thread_id,
            name=t.name,
            parent_id=parent_map.get(t.thread_id),
            model_key=model,
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

    # Bulk fetch model settings
    model_map: Dict[str, str] = {}
    try:
        cur = core.db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
        for row in cur.fetchall():
            model_map[row[0]] = row[1]
    except Exception:
        pass

    threads = []
    for t in all_threads:
        # Skip if thread has a parent (not a root)
        if t.thread_id in parent_set:
            continue
        model = model_map.get(t.thread_id) or t.initial_model_key
        threads.append(ThreadInfo(
            id=t.thread_id,
            name=t.name,
            parent_id=None,
            model_key=model,
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
        has_children=len(children) > 0,
    )


@router.post("", response_model=ThreadInfo)
async def create_thread(request: CreateThreadRequest):
    """Create a new thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    model_key = request.model_key or core.default_model_key or next(iter(core.models_config.keys()), None)

    models_path = str(core.PROJECT_ROOT / "egg" / "models.json")

    if request.parent_id:
        # Create child thread
        thread_id = create_child_thread(
            core.db,
            parent_id=request.parent_id,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
        )
    else:
        # Create root thread
        thread_id = create_root_thread(
            core.db,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
        )

    t = core.db.get_thread(thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(core.db, t.thread_id),
        model_key=current_thread_model(core.db, t.thread_id),
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
            has_children=len(grandchildren) > 0,
        ))
    return children


@router.get("/{thread_id}/state")
async def get_thread_state_endpoint(thread_id: str):
    """Get the current state of a thread (running, waiting, etc.)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    state = thread_state(core.db, thread_id)
    root_id = get_thread_root_id(thread_id)

    return {
        "state": state,
        "scheduler_running": root_id in core.active_schedulers,
    }
