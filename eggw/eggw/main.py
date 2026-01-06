"""FastAPI backend for eggw - Web UI for eggthreads."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

# Add parent directories to path for eggthreads/eggllm imports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "eggthreads"))
sys.path.insert(0, str(PROJECT_ROOT / "eggllm"))

import eggthreads
from eggthreads import (
    ThreadsDB,
    SubtreeScheduler,
    EventWatcher,
    create_root_thread,
    create_child_thread,
    append_message,
    delete_thread,
    list_threads,
    list_root_threads,
    list_children_ids,
    list_children_with_meta,
    get_parent,
    build_tool_call_states,
    thread_state,
    current_thread_model,
    set_thread_model,
    duplicate_thread,
    approve_tool_calls_for_thread,
    total_token_stats,
    execute_bash_command,
    interrupt_thread,
    # Sandbox functions
    get_thread_sandbox_status,
    get_thread_sandbox_config,
    set_thread_sandbox_config,
    is_user_sandbox_control_enabled,
    get_sandbox_status,
    # Tools config functions
    disable_tool_for_thread,
    enable_tool_for_thread,
    set_thread_allow_raw_tool_output,
    get_thread_tools_config,
)

from models import (
    ThreadInfo,
    MessageContent,
    ToolCallInfo,
    SendMessageRequest,
    CreateThreadRequest,
    SetModelRequest,
    ApprovalRequest,
    ThreadTokenStats,
    ModelInfo,
    ModelsResponse,
    CommandRequest,
    CommandResponse,
)

# Global state
db: Optional[ThreadsDB] = None
llm_client = None
models_config: Dict[str, Any] = {}
default_model_key: Optional[str] = None
active_schedulers: Dict[str, Dict[str, Any]] = {}  # root_thread_id -> {"scheduler": SubtreeScheduler, "task": Task}
MODELS_PATH = PROJECT_ROOT / "egg" / "models.json"
ALL_MODELS_PATH = PROJECT_ROOT / "egg" / "all-models.json"


def load_models_config() -> tuple[Dict[str, Any], Optional[str]]:
    """Load models configuration using eggllm's config loader."""
    from eggllm.config import load_models_config as eggllm_load_models

    models_path = PROJECT_ROOT / "egg" / "models.json"
    if not models_path.exists():
        return {}, None

    models_config, _ = eggllm_load_models(models_path)

    # Get default_model from the raw JSON
    default_model = None
    try:
        import json
        with open(models_path) as f:
            raw_config = json.load(f)
            default_model = raw_config.get("default_model")
    except:
        pass

    return models_config, default_model


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db, scheduler, llm_client, models_config, default_model_key

    # Change to the caller's working directory if specified
    # This ensures sandbox configs, models.json, etc. are found correctly
    egg_cwd = os.environ.get("EGG_CWD")
    if egg_cwd:
        os.chdir(egg_cwd)
        print(f"Working directory: {egg_cwd}")

    # Initialize database - use EGG_DB_PATH if specified, else default
    db_path = os.environ.get("EGG_DB_PATH")
    if db_path:
        db = ThreadsDB(db_path)
        print(f"Database: {db_path}")
    else:
        db = ThreadsDB()  # Uses default .egg/threads.sqlite
    db.init_schema()  # Create tables if they don't exist

    # Load models
    models_config, default_model_key = load_models_config()

    # Initialize LLM client
    # Look for models.json in CWD first, then fall back to egg directory
    cwd_models = Path.cwd() / "models.json"
    egg_models = PROJECT_ROOT / "egg" / "models.json"
    models_path = cwd_models if cwd_models.exists() else egg_models
    try:
        from eggllm import LLMClient
        llm_client = LLMClient(models_path=models_path)
        print(f"Models config: {models_path}")
    except Exception as e:
        print(f"Warning: Could not initialize LLM client: {e}")
        llm_client = None

    # Note: SubtreeScheduler requires a root_thread_id to watch.
    # For per-thread scheduling, we'll start schedulers on-demand when messages are sent.
    # Global scheduler initialization is skipped for now.

    yield

    # Cleanup
    pass


def get_thread_root_id(thread_id: str) -> str:
    """Return the root thread id for any thread id."""
    if not db:
        return thread_id
    current = thread_id
    while True:
        parent = get_parent(db, current)
        if not parent:
            return current
        current = parent


def start_scheduler(root_tid: str) -> None:
    """Start a scheduler for a root thread if not already running."""
    global active_schedulers

    if root_tid in active_schedulers:
        return  # Already running

    if not db:
        return

    poll_sec = float(os.environ.get("EGG_POLL_SEC", "0.15"))

    sched = SubtreeScheduler(
        db,
        root_thread_id=root_tid,
        models_path=str(MODELS_PATH),
        all_models_path=str(ALL_MODELS_PATH),
    )
    task = asyncio.create_task(sched.run_forever(poll_sec=poll_sec))
    active_schedulers[root_tid] = {"scheduler": sched, "task": task}
    print(f"Started scheduler for root {root_tid[-8:]}")


def stop_scheduler(root_tid: str) -> None:
    """Stop a scheduler for a root thread."""
    global active_schedulers

    if root_tid not in active_schedulers:
        return

    entry = active_schedulers.pop(root_tid)
    task = entry.get("task")

    if task and not task.done():
        task.cancel()

    print(f"Stopped scheduler for root {root_tid[-8:]}")


def ensure_scheduler_for(thread_id: str) -> None:
    """Ensure a scheduler is running for the thread's root."""
    root_id = get_thread_root_id(thread_id)
    if root_id not in active_schedulers:
        start_scheduler(root_id)


app = FastAPI(
    title="eggw API",
    description="Web API for eggthreads",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Thread endpoints ---

@app.get("/api/threads", response_model=List[ThreadInfo])
async def get_threads():
    """List all threads (optimized with bulk queries)."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    all_threads = list_threads(db)
    if not all_threads:
        return []

    # Bulk fetch parent-child relationships
    children_set: set[str] = set()  # threads that have children
    parent_map: Dict[str, str] = {}  # child_id -> parent_id
    try:
        cur = db.conn.execute("SELECT parent_id, child_id FROM children")
        for row in cur.fetchall():
            children_set.add(row[0])  # parent has children
            parent_map[row[1]] = row[0]  # child -> parent
    except Exception:
        pass

    # Bulk fetch model settings
    model_map: Dict[str, str] = {}
    try:
        cur = db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
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


@app.get("/api/threads/roots", response_model=List[ThreadInfo])
async def get_root_threads():
    """List only root threads (optimized with bulk queries)."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    all_threads = list_threads(db)
    if not all_threads:
        return []

    # Bulk fetch parent-child relationships
    children_set: set[str] = set()  # threads that have children
    parent_set: set[str] = set()  # threads that have a parent
    try:
        cur = db.conn.execute("SELECT parent_id, child_id FROM children")
        for row in cur.fetchall():
            children_set.add(row[0])
            parent_set.add(row[1])
    except Exception:
        pass

    # Bulk fetch model settings
    model_map: Dict[str, str] = {}
    try:
        cur = db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
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


@app.get("/api/threads/{thread_id}", response_model=ThreadInfo)
async def get_thread(thread_id: str):
    """Get a specific thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    children = list_children_ids(db, t.thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(db, t.thread_id),
        model_key=current_thread_model(db, t.thread_id),
        has_children=len(children) > 0,
    )


@app.post("/api/threads", response_model=ThreadInfo)
async def create_thread(request: CreateThreadRequest):
    """Create a new thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    model_key = request.model_key or default_model_key or next(iter(models_config.keys()), None)

    models_path = str(PROJECT_ROOT / "egg" / "models.json")

    if request.parent_id:
        # Create child thread
        thread_id = create_child_thread(
            db,
            parent_id=request.parent_id,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
        )
    else:
        # Create root thread
        thread_id = create_root_thread(
            db,
            name=request.name,
            initial_model_key=model_key,
            models_path=models_path,
        )

    t = db.get_thread(thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(db, t.thread_id),
        model_key=current_thread_model(db, t.thread_id),
        has_children=False,
    )


@app.patch("/api/threads/{thread_id}")
async def update_thread(thread_id: str, name: Optional[str] = None):
    """Update thread properties (e.g., name)."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    if name is not None:
        db.conn.execute(
            "UPDATE threads SET name = ? WHERE thread_id = ?",
            (name, thread_id)
        )
        db.conn.commit()

    t = db.get_thread(thread_id)
    children = list_children_ids(db, t.thread_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(db, t.thread_id),
        model_key=current_thread_model(db, t.thread_id),
        has_children=len(children) > 0,
    )


@app.delete("/api/threads/{thread_id}")
async def remove_thread(thread_id: str, delete_subtree: bool = False):
    """Delete a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    delete_thread(db, thread_id, delete_subtree=delete_subtree)
    return {"status": "deleted"}


@app.post("/api/threads/{thread_id}/duplicate", response_model=ThreadInfo)
async def duplicate_thread_endpoint(thread_id: str, name: Optional[str] = None):
    """Duplicate a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    new_id = duplicate_thread(db, thread_id, new_name=name)
    t = db.get_thread(new_id)
    return ThreadInfo(
        id=t.thread_id,
        name=t.name,
        parent_id=get_parent(db, t.thread_id),
        model_key=current_thread_model(db, t.thread_id),
        has_children=False,
    )


@app.get("/api/threads/{thread_id}/children", response_model=List[ThreadInfo])
async def get_thread_children(thread_id: str):
    """Get children of a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    children = []
    # list_children_with_meta returns tuples: (child_id, name, short_recap, created_at)
    for child_id, name, short_recap, created_at in list_children_with_meta(db, thread_id):
        grandchildren = list_children_ids(db, child_id)
        children.append(ThreadInfo(
            id=child_id,
            name=name,
            parent_id=thread_id,
            model_key=current_thread_model(db, child_id),
            has_children=len(grandchildren) > 0,
        ))
    return children


# --- Message endpoints ---

@app.get("/api/threads/{thread_id}/messages", response_model=List[MessageContent])
async def get_messages(thread_id: str):
    """Get messages for a thread from snapshot."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages = []
    if t.snapshot_json:
        try:
            snap = json.loads(t.snapshot_json)
            for msg in snap.get("messages", []):
                messages.append(MessageContent(
                    id=msg.get("id", ""),
                    role=msg.get("role", ""),
                    content=msg.get("content"),
                    reasoning=msg.get("reasoning"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    model_key=msg.get("model_key"),
                ))
        except json.JSONDecodeError:
            pass

    return messages


@app.post("/api/threads/{thread_id}/messages")
async def send_message(thread_id: str, request: SendMessageRequest):
    """Send a message to a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Append user message
    msg_id = append_message(db, thread_id, role="user", content=request.content)

    # Ensure scheduler is running for this thread's root
    ensure_scheduler_for(thread_id)

    return {"status": "sent", "message_id": msg_id}


@app.post("/api/threads/{thread_id}/open")
async def open_thread(thread_id: str):
    """Open a thread for viewing. Stops any running scheduler to prevent auto-streaming."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Stop all active schedulers when switching threads.
    # This prevents auto-streaming when navigating to a thread with pending work.
    # Scheduler will only restart when user explicitly sends a message,
    # approves a tool, or runs a shell command.
    for root_id in list(active_schedulers.keys()):
        stop_scheduler(root_id)

    root_id = get_thread_root_id(thread_id)
    return {
        "status": "ok",
        "thread_id": thread_id,
        "root_id": root_id,
        "scheduler_running": False,  # Always false now since we stopped them
    }


@app.post("/api/threads/{thread_id}/interrupt")
async def interrupt_thread_endpoint(thread_id: str):
    """Interrupt/cancel current streaming or pending work (Ctrl+C equivalent)."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Interrupt the thread
    result = interrupt_thread(db, thread_id, reason="user")

    return {
        "status": "interrupted",
        "thread_id": thread_id,
        "invoke_id": result,
    }


# --- Command endpoints ---

@app.post("/api/threads/{thread_id}/command", response_model=CommandResponse)
async def execute_command(thread_id: str, request: CommandRequest):
    """Execute a slash command or shell command."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    cmd = request.command.strip()

    # Handle shell commands: $$ (hidden) or $ (visible)
    if cmd.startswith('$$') and len(cmd) > 2:
        return await _execute_bash_command(thread_id, cmd[2:].strip(), hidden=True)
    elif cmd.startswith('$') and len(cmd) > 1:
        return await _execute_bash_command(thread_id, cmd[1:].strip(), hidden=False)

    # Handle slash commands
    if cmd.startswith('/'):
        parts = cmd[1:].split(None, 1)
        command_name = parts[0] if parts else ""
        command_arg = parts[1] if len(parts) > 1 else ""

        # Dispatch to command handlers
        if command_name == "model":
            return await _cmd_model(thread_id, command_arg)
        elif command_name == "spawn" or command_name == "spawnChildThread":
            return await _cmd_spawn(thread_id, command_arg)
        elif command_name == "newThread":
            return await _cmd_new_thread(command_arg)
        elif command_name == "help":
            return _cmd_help()
        elif command_name == "toggleAutoApproval":
            return await _cmd_toggle_auto_approval(thread_id)
        elif command_name == "parentThread":
            return await _cmd_parent_thread(thread_id)
        elif command_name == "thread":
            return await _cmd_switch_thread(command_arg)
        elif command_name == "threads":
            return await _cmd_list_threads()
        elif command_name == "listChildren":
            return await _cmd_list_children(thread_id)
        elif command_name == "deleteThread":
            return await _cmd_delete_thread(thread_id, command_arg)
        elif command_name == "duplicateThread":
            return await _cmd_duplicate_thread(thread_id, command_arg)
        elif command_name == "rename":
            return await _cmd_rename(thread_id, command_arg)
        elif command_name == "cost":
            return await _cmd_cost(thread_id)
        elif command_name == "toolsOn":
            return await _cmd_tools_on(thread_id)
        elif command_name == "toolsOff":
            return await _cmd_tools_off(thread_id)
        elif command_name == "toolsStatus":
            return await _cmd_tools_status(thread_id)
        elif command_name == "schedulers":
            return _cmd_schedulers()
        elif command_name == "toggleSandboxing":
            return await _cmd_toggle_sandboxing(thread_id)
        elif command_name == "setSandboxConfiguration":
            return await _cmd_set_sandbox_configuration(thread_id, command_arg)
        elif command_name == "getSandboxingConfig":
            return await _cmd_get_sandboxing_config(thread_id)
        # P1 Commands
        elif command_name == "updateAllModels":
            return await _cmd_update_all_models(command_arg)
        elif command_name == "disableTool":
            return await _cmd_disable_tool(thread_id, command_arg)
        elif command_name == "enableTool":
            return await _cmd_enable_tool(thread_id, command_arg)
        elif command_name == "spawnAutoApprovedChildThread":
            return await _cmd_spawn_auto_approved(thread_id, command_arg)
        # P2 Commands
        elif command_name == "toolsSecrets":
            return await _cmd_tools_secrets(thread_id, command_arg)
        elif command_name == "waitForThreads":
            return await _cmd_wait_for_threads(thread_id, command_arg)
        elif command_name == "togglePanel":
            return _cmd_toggle_panel(command_arg)
        # P3 Commands
        elif command_name == "paste":
            return _cmd_paste()
        elif command_name == "enterMode":
            return _cmd_enter_mode(command_arg)
        elif command_name == "toggleBorders":
            return _cmd_toggle_borders()
        elif command_name == "theme":
            return _cmd_theme(command_arg)
        elif command_name == "quit":
            return _cmd_quit()
        else:
            return CommandResponse(
                success=False,
                message=f"Unknown command: /{command_name}",
            )

    return CommandResponse(success=False, message="Invalid command format")


async def _execute_bash_command(thread_id: str, script: str, hidden: bool) -> CommandResponse:
    """Execute a bash command as a tool call."""
    if not script:
        return CommandResponse(success=False, message="Empty bash command")

    # Use eggthreads' execute_bash_command which handles everything correctly
    tc_id = execute_bash_command(db, thread_id, script, hidden=hidden)

    # Ensure scheduler is running
    ensure_scheduler_for(thread_id)

    return CommandResponse(
        success=True,
        message=f"Executing: {script}",
        data={"tool_call_id": tc_id, "hidden": hidden},
    )


async def _cmd_model(thread_id: str, model_name: str) -> CommandResponse:
    """Handle /model command."""
    if not model_name:
        # Return current model
        current = current_thread_model(db, thread_id)
        return CommandResponse(
            success=True,
            message=f"Current model: {current}",
            data={"model_key": current},
        )

    # Check if model exists
    if model_name not in models_config:
        # Try partial match
        matches = [k for k in models_config.keys() if model_name.lower() in k.lower()]
        if len(matches) == 1:
            model_name = matches[0]
        elif len(matches) > 1:
            return CommandResponse(
                success=False,
                message=f"Ambiguous model name. Matches: {', '.join(matches[:5])}",
            )
        else:
            return CommandResponse(
                success=False,
                message=f"Unknown model: {model_name}",
            )

    set_thread_model(db, thread_id, model_name)
    return CommandResponse(
        success=True,
        message=f"Model changed to: {model_name}",
        data={"model_key": model_name},
    )


async def _cmd_spawn(thread_id: str, context: str) -> CommandResponse:
    """Handle /spawn or /spawnChildThread command."""
    models_path = str(PROJECT_ROOT / "egg" / "models.json")

    # Get parent's model
    parent_model = current_thread_model(db, thread_id)

    # Create child thread
    child_id = create_child_thread(
        db,
        parent_id=thread_id,
        initial_model_key=parent_model,
        models_path=models_path,
    )

    # If context provided, add it as a user message
    if context.strip():
        append_message(db, child_id, 'user', context.strip())
        ensure_scheduler_for(child_id)

    return CommandResponse(
        success=True,
        message=f"Spawned child thread: {child_id[-8:]}",
        data={"child_id": child_id, "parent_id": thread_id},
    )


async def _cmd_new_thread(name: str) -> CommandResponse:
    """Handle /newThread command."""
    models_path = str(PROJECT_ROOT / "egg" / "models.json")
    model_key = default_model_key or next(iter(models_config.keys()), None)

    thread_id = create_root_thread(
        db,
        name=name if name else None,
        initial_model_key=model_key,
        models_path=models_path,
    )

    return CommandResponse(
        success=True,
        message=f"Created new thread: {thread_id[-8:]}",
        data={"thread_id": thread_id},
    )


def get_auto_approval_status(thread_id: str) -> bool:
    """Check if auto-approval is currently active for a thread.

    This scans the tool_call.approval events to find the current state.
    """
    if not db:
        return False

    # Scan events for global_approval/revoke_global_approval
    cur = db.conn.execute(
        """SELECT payload_json FROM events
           WHERE thread_id=? AND type='tool_call.approval'
           ORDER BY event_seq DESC""",
        (thread_id,)
    )

    for row in cur.fetchall():
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            decision = payload.get("decision")
            if decision == "global_approval":
                return True
            if decision == "revoke_global_approval":
                return False
        except:
            continue

    return False


async def _cmd_toggle_auto_approval(thread_id: str) -> CommandResponse:
    """Handle /toggleAutoApproval command."""
    current_state = get_auto_approval_status(thread_id)
    new_state = not current_state

    # Use the appropriate decision
    decision = "global_approval" if new_state else "revoke_global_approval"
    reason = f"Auto-approval {'enabled' if new_state else 'disabled'} via web UI"

    approve_tool_calls_for_thread(db, thread_id, decision=decision, reason=reason)

    return CommandResponse(
        success=True,
        message=f"Auto-approval {'enabled' if new_state else 'disabled'}",
        data={"auto_approval": new_state},
    )


async def _cmd_parent_thread(thread_id: str) -> CommandResponse:
    """Handle /parentThread command."""
    parent_id = get_parent(db, thread_id)
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


async def _cmd_switch_thread(selector: str) -> CommandResponse:
    """Handle /thread command to switch to a thread by ID, partial ID, name, or recap."""
    if not selector:
        return CommandResponse(success=False, message="Usage: /thread <id or partial-id or name or recap>")

    # Try exact match first
    t = db.get_thread(selector)
    if t:
        return CommandResponse(
            success=True,
            message=f"Switched to thread: {selector[-8:]}",
            data={"thread_id": selector},
        )

    # Try partial match on id, name, and recap (case-insensitive)
    all_threads = list_threads(db)
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


async def _cmd_list_threads() -> CommandResponse:
    """Handle /threads command - shows thread tree structure (optimized)."""
    # Fetch all data in bulk to avoid N+1 queries
    all_threads = list_threads(db)
    if not all_threads:
        return CommandResponse(success=True, message="No threads found")

    # Build lookup maps
    threads_by_id = {t.thread_id: t for t in all_threads}

    # Fetch all parent-child relationships in one query
    children_map: Dict[str, List[str]] = {}  # parent_id -> [child_ids]
    parent_map: Dict[str, str] = {}  # child_id -> parent_id
    try:
        cur = db.conn.execute("SELECT parent_id, child_id FROM children")
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
        cur = db.conn.execute("SELECT thread_id, value FROM thread_config WHERE key = 'model_key'")
        for row in cur.fetchall():
            model_map[row[0]] = row[1]
    except Exception:
        pass

    # For threads without explicit model, use initial_model_key
    for t in all_threads:
        if t.thread_id not in model_map and t.initial_model_key:
            model_map[t.thread_id] = t.initial_model_key

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
        state = t.status if t.status != "waiting_user" else ""
        state_part = f" <{state}>" if state else ""

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


async def _cmd_list_children(thread_id: str) -> CommandResponse:
    """Handle /listChildren command."""
    children = list_children_with_meta(db, thread_id)
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


async def _cmd_delete_thread(current_thread_id: str, selector: str) -> CommandResponse:
    """Handle /deleteThread command."""
    target_id = selector.strip() if selector else current_thread_id

    # Try to find the thread
    t = db.get_thread(target_id)
    if not t:
        # Try partial match
        all_threads = list_threads(db)
        matches = [th for th in all_threads if target_id.lower() in th.thread_id.lower()]
        if len(matches) == 1:
            target_id = matches[0].thread_id
        elif len(matches) > 1:
            return CommandResponse(success=False, message="Ambiguous thread selector")
        else:
            return CommandResponse(success=False, message="Thread not found")

    delete_thread(db, target_id, delete_subtree=True)
    return CommandResponse(
        success=True,
        message=f"Deleted thread: {target_id[-8:]}",
        data={"deleted_id": target_id},
    )


async def _cmd_duplicate_thread(thread_id: str, name: str) -> CommandResponse:
    """Handle /duplicateThread command."""
    new_id = duplicate_thread(db, thread_id, new_name=name if name else None)
    return CommandResponse(
        success=True,
        message=f"Duplicated to: {new_id[-8:]}",
        data={"thread_id": new_id, "source_id": thread_id},
    )


async def _cmd_rename(thread_id: str, new_name: str) -> CommandResponse:
    """Handle /rename command."""
    if not new_name:
        t = db.get_thread(thread_id)
        current_name = t.name if t and t.name else "(no name)"
        return CommandResponse(
            success=False,
            message=f"Usage: /rename <new name>\nCurrent name: {current_name}",
        )

    db.conn.execute(
        "UPDATE threads SET name = ? WHERE thread_id = ?",
        (new_name, thread_id)
    )
    db.conn.commit()

    return CommandResponse(
        success=True,
        message=f"Thread renamed to: {new_name}",
        data={"name": new_name},
    )


async def _cmd_cost(thread_id: str) -> CommandResponse:
    """Handle /cost command."""
    stats = total_token_stats(db, thread_id, llm=llm_client)
    api_usage = stats.get("api_usage", {})
    totals = api_usage.get("totals", {})
    cost_info = api_usage.get("cost_usd", {})

    input_tokens = totals.get("input_tokens", 0)
    output_tokens = totals.get("output_tokens", 0)
    reasoning_tokens = totals.get("reasoning_tokens", 0)
    cached_tokens = totals.get("cached_tokens", 0)
    cost_usd = cost_info.get("total", 0)

    return CommandResponse(
        success=True,
        message=f"Tokens: {input_tokens:,} in, {output_tokens:,} out, {reasoning_tokens:,} reasoning, {cached_tokens:,} cached\nCost: ${cost_usd:.4f}",
        data={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": cost_usd,
        },
    )


async def _cmd_tools_on(thread_id: str) -> CommandResponse:
    """Handle /toolsOn command - enable all tools."""
    try:
        from eggthreads import set_thread_tools_enabled
        set_thread_tools_enabled(db, thread_id, True)
        return CommandResponse(success=True, message="Tools enabled for this thread")
    except Exception as e:
        return CommandResponse(success=False, message=f"Error: {e}")


async def _cmd_tools_off(thread_id: str) -> CommandResponse:
    """Handle /toolsOff command - disable all tools."""
    try:
        from eggthreads import set_thread_tools_enabled
        set_thread_tools_enabled(db, thread_id, False)
        return CommandResponse(success=True, message="Tools disabled for this thread")
    except Exception as e:
        return CommandResponse(success=False, message=f"Error: {e}")


async def _cmd_tools_status(thread_id: str) -> CommandResponse:
    """Handle /toolsStatus command."""
    try:
        from eggthreads import get_thread_tools_config
        cfg = get_thread_tools_config(db, thread_id)
        status = "enabled" if cfg.llm_tools_enabled else "disabled"
        disabled = sorted(cfg.disabled_tools) if cfg.disabled_tools else []
        disabled_str = ", ".join(disabled) if disabled else "(none)"
        return CommandResponse(
            success=True,
            message=f"Tools: {status}\nDisabled: {disabled_str}",
            data={"enabled": cfg.llm_tools_enabled, "disabled": disabled},
        )
    except Exception as e:
        # Fallback to just listing tool calls
        states = build_tool_call_states(db, thread_id)
        if not states:
            return CommandResponse(success=True, message="No tool calls in this thread")

        lines = []
        for tc_id, tc in states.items():
            lines.append(f"  {tc.name} [{tc.state}] - {tc_id[-8:]}")

        return CommandResponse(
            success=True,
            message=f"Tool calls ({len(states)}):\n" + "\n".join(lines),
            data={"count": len(states)},
        )


def _cmd_schedulers() -> CommandResponse:
    """Handle /schedulers command."""
    if not active_schedulers:
        return CommandResponse(success=True, message="No active schedulers")

    lines = []
    for root_id in active_schedulers:
        lines.append(f"  {root_id[-8:]}")

    return CommandResponse(
        success=True,
        message=f"Active schedulers ({len(active_schedulers)}):\n" + "\n".join(lines),
        data={"count": len(active_schedulers), "roots": list(active_schedulers.keys())},
    )


async def _cmd_toggle_sandboxing(thread_id: str) -> CommandResponse:
    """Handle /toggleSandboxing command - toggle sandboxing for thread subtree."""
    # Check if user sandbox control is enabled
    try:
        if not is_user_sandbox_control_enabled(db, thread_id):
            return CommandResponse(
                success=False,
                message="User sandbox control is disabled for this thread.",
            )
    except Exception:
        pass  # Older version, assume enabled

    try:
        st = get_thread_sandbox_status(db, thread_id)
        enabled_before = bool(st.get('enabled'))
        new_enabled = not enabled_before

        # Toggle only the enabled flag while keeping current settings
        cfg = get_thread_sandbox_config(db, thread_id)
        set_thread_sandbox_config(
            db,
            thread_id,
            enabled=new_enabled,
            settings=cfg.settings,
            reason='/toggleSandboxing',
        )

        st2 = get_thread_sandbox_status(db, thread_id)
        effective = bool(st2.get('effective'))
        warning = st2.get('warning')

        if effective:
            return CommandResponse(
                success=True,
                message='Sandboxing ENABLED for this thread subtree.',
                data={"enabled": True, "effective": True},
            )
        elif new_enabled:
            msg = 'Sandboxing ENABLED but not effective'
            if warning:
                msg += f": {warning}"
            return CommandResponse(
                success=True,
                message=msg,
                data={"enabled": True, "effective": False, "warning": warning},
            )
        else:
            return CommandResponse(
                success=True,
                message='Sandboxing DISABLED for this thread subtree.',
                data={"enabled": False, "effective": False},
            )
    except Exception as e:
        return CommandResponse(success=False, message=f'/toggleSandboxing error: {e}')


async def _cmd_set_sandbox_configuration(thread_id: str, config_name: str) -> CommandResponse:
    """Handle /setSandboxConfiguration command - apply sandbox config file."""
    # Check if user sandbox control is enabled
    try:
        if not is_user_sandbox_control_enabled(db, thread_id):
            return CommandResponse(
                success=False,
                message="User sandbox control is disabled for this thread.",
            )
    except Exception:
        pass  # Older version, assume enabled

    name = config_name.strip()
    if not name:
        # Return help message
        return CommandResponse(
            success=True,
            message="""Sandbox Configuration Commands:
  /toggleSandboxing - Toggle sandbox on/off for this thread
  /setSandboxConfiguration <file.json> - Apply config from .egg/sandbox/
  /getSandboxingConfig - Show current sandbox configuration

Config files are stored in .egg/sandbox/ directory.
Use tab completion to see available configs.""",
        )

    try:
        set_thread_sandbox_config(
            db,
            thread_id,
            enabled=True,
            config_name=name,
            reason='/setSandboxConfiguration',
        )
        return CommandResponse(
            success=True,
            message=f"Sandbox configuration applied: {name}",
            data={"config_name": name},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f'/setSandboxConfiguration error: {e}')


async def _cmd_get_sandboxing_config(thread_id: str) -> CommandResponse:
    """Handle /getSandboxingConfig command - show current sandbox config."""
    try:
        sb = get_thread_sandbox_status(db, thread_id)
        lines = [
            "Current thread sandbox configuration:",
            f"  Provider: {sb.get('provider', 'unknown')}",
            f"  Enabled: {sb.get('enabled', False)}",
            f"  Available: {sb.get('available', False)}",
            f"  Effective: {sb.get('effective', False)}",
            f"  Config source: {sb.get('config_source', 'unknown')}",
        ]
        config_path = sb.get('config_path')
        if config_path:
            lines.append(f"  Config path: {config_path}")
        warning = sb.get('warning')
        if warning:
            lines.append(f"  Warning: {warning}")

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "provider": sb.get('provider'),
                "enabled": sb.get('enabled'),
                "available": sb.get('available'),
                "effective": sb.get('effective'),
                "warning": warning,
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f'/getSandboxingConfig error: {e}')


# --- P1 Commands ---

async def _cmd_update_all_models(provider: str) -> CommandResponse:
    """Handle /updateAllModels command - refresh model catalog for a provider."""
    provider = provider.strip()
    if not provider:
        return CommandResponse(
            success=True,
            message="Usage: /updateAllModels <provider>\nAvailable providers: openai, anthropic, google, etc.",
        )

    try:
        if llm_client:
            count = llm_client.catalog.update_models_for_provider(provider)
            return CommandResponse(
                success=True,
                message=f"Updated {count} models for provider '{provider}'",
                data={"provider": provider, "count": count},
            )
        else:
            return CommandResponse(success=False, message="LLM client not initialized")
    except Exception as e:
        return CommandResponse(success=False, message=f"/updateAllModels error: {e}")


async def _cmd_disable_tool(thread_id: str, tool_name: str) -> CommandResponse:
    """Handle /disableTool command - disable a specific tool for thread."""
    name = tool_name.strip()
    if not name:
        return CommandResponse(
            success=True,
            message="Usage: /disableTool <tool_name>",
        )

    try:
        disable_tool_for_thread(db, thread_id, name)
        return CommandResponse(
            success=True,
            message=f"Disabled tool: {name}",
            data={"tool_name": name, "enabled": False},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/disableTool error: {e}")


async def _cmd_enable_tool(thread_id: str, tool_name: str) -> CommandResponse:
    """Handle /enableTool command - enable a specific tool for thread."""
    name = tool_name.strip()
    if not name:
        return CommandResponse(
            success=True,
            message="Usage: /enableTool <tool_name>",
        )

    try:
        enable_tool_for_thread(db, thread_id, name)
        return CommandResponse(
            success=True,
            message=f"Enabled tool: {name}",
            data={"tool_name": name, "enabled": True},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/enableTool error: {e}")


async def _cmd_spawn_auto_approved(thread_id: str, context: str) -> CommandResponse:
    """Handle /spawnAutoApprovedChildThread command - spawn child with global auto-approval."""
    try:
        # Get current thread's model
        current_model = current_thread_model(db, thread_id)

        # Create child thread
        child_id = create_child_thread(
            db,
            parent_id=thread_id,
            initial_model_key=current_model,
        )

        # Enable global auto-approval for the child thread
        approve_tool_calls_for_thread(db, child_id, decision="global_approval")

        # Add context as user message if provided
        ctx = context.strip()
        if ctx:
            append_message(db, child_id, "user", ctx)

        return CommandResponse(
            success=True,
            message=f"Spawned auto-approved child thread: {child_id[-8:]}",
            data={"child_id": child_id, "auto_approved": True},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/spawnAutoApprovedChildThread error: {e}")


# --- P2 Commands ---

async def _cmd_tools_secrets(thread_id: str, mode: str) -> CommandResponse:
    """Handle /toolsSecrets command - toggle secrets masking in tool output."""
    mode = mode.strip().lower()
    if mode not in ("on", "off"):
        return CommandResponse(
            success=True,
            message="Usage: /toolsSecrets <on|off>\n  on = allow raw tool output (secrets visible)\n  off = mask detected secrets",
        )

    try:
        allow_raw = mode == "on"
        set_thread_allow_raw_tool_output(db, thread_id, allow_raw)
        if allow_raw:
            return CommandResponse(
                success=True,
                message="Tool output secrets: raw mode ENABLED (secrets will not be masked)",
                data={"allow_raw": True},
            )
        else:
            return CommandResponse(
                success=True,
                message="Tool output secrets: masking ENABLED (attempting to mask detected secrets)",
                data={"allow_raw": False},
            )
    except Exception as e:
        return CommandResponse(success=False, message=f"/toolsSecrets error: {e}")


async def _cmd_wait_for_threads(thread_id: str, thread_selectors: str) -> CommandResponse:
    """Handle /waitForThreads command - wait for specified threads to complete."""
    selectors = thread_selectors.strip()
    if not selectors:
        return CommandResponse(
            success=True,
            message="Usage: /waitForThreads <thread_id>[,<thread_id>...]\nWait for specified child threads to reach waiting_user state.",
        )

    # Parse thread selectors (comma-separated)
    thread_ids = [s.strip() for s in selectors.split(",") if s.strip()]

    # Resolve each selector to a thread ID
    resolved_ids = []
    for selector in thread_ids:
        # Try to find thread by ID suffix or name
        found = None
        for t in list_threads(db):
            if t["id"].endswith(selector) or t.get("name") == selector:
                found = t["id"]
                break
        if found:
            resolved_ids.append(found)
        else:
            return CommandResponse(
                success=False,
                message=f"Thread not found: {selector}",
            )

    # Check current states
    states = {}
    all_ready = True
    for tid in resolved_ids:
        state = thread_state(db, tid)
        states[tid[-8:]] = state
        if state not in ("waiting_user", "paused"):
            all_ready = False

    if all_ready:
        return CommandResponse(
            success=True,
            message=f"All {len(resolved_ids)} threads are ready",
            data={"states": states, "all_ready": True},
        )
    else:
        return CommandResponse(
            success=True,
            message=f"Waiting for threads: {states}",
            data={"states": states, "all_ready": False, "waiting": True},
        )


def _cmd_toggle_panel(panel_name: str) -> CommandResponse:
    """Handle /togglePanel command - toggle panel visibility (frontend-only)."""
    name = panel_name.strip().lower()
    valid_panels = ["chat", "children", "system"]
    if name not in valid_panels:
        return CommandResponse(
            success=True,
            message=f"Usage: /togglePanel <{'/'.join(valid_panels)}>",
        )

    # This is handled client-side, we just return which panel to toggle
    return CommandResponse(
        success=True,
        message=f"Toggle panel: {name}",
        data={"panel": name, "action": "toggle"},
    )


# --- P3 Commands ---

def _cmd_paste() -> CommandResponse:
    """Handle /paste command - paste from clipboard (frontend-only)."""
    return CommandResponse(
        success=True,
        message="Use Ctrl+V or Cmd+V to paste from clipboard",
        data={"action": "paste"},
    )


def _cmd_enter_mode(mode: str) -> CommandResponse:
    """Handle /enterMode command - set Enter key behavior (frontend-only)."""
    mode = mode.strip().lower()
    if mode not in ("send", "newline"):
        return CommandResponse(
            success=True,
            message="Usage: /enterMode <send|newline>\n  send = Enter sends message (Shift+Enter for newline)\n  newline = Enter inserts newline (Ctrl+Enter to send)",
        )

    return CommandResponse(
        success=True,
        message=f"Enter mode set to: {mode}",
        data={"enter_mode": mode},
    )


def _cmd_toggle_borders() -> CommandResponse:
    """Handle /toggleBorders command - toggle panel borders (frontend-only)."""
    return CommandResponse(
        success=True,
        message="Panel borders toggled",
        data={"action": "toggle_borders"},
    )


def _cmd_quit() -> CommandResponse:
    """Handle /quit command - exit the application."""
    return CommandResponse(
        success=True,
        message="To exit eggw, close this browser tab or press Ctrl+C in the terminal running eggw.sh",
        data={"action": "quit"},
    )


# Available themes
THEMES = ["dark", "light", "light-mono", "midnight", "cyberpunk", "forest", "ocean", "sunset", "mono", "disney", "fruit", "vegetables", "coffee", "matrix"]


def _cmd_theme(theme_name: str) -> CommandResponse:
    """Handle /theme command - change color scheme."""
    if not theme_name:
        # List available themes
        return CommandResponse(
            success=True,
            message=f"Available themes: {', '.join(THEMES)}\nUse /theme <name> to switch",
            data={"themes": THEMES, "action": "list_themes"},
        )

    theme = theme_name.lower().strip()
    if theme not in THEMES:
        return CommandResponse(
            success=False,
            message=f"Unknown theme: {theme}. Available: {', '.join(THEMES)}",
        )

    return CommandResponse(
        success=True,
        message=f"Theme changed to: {theme}",
        data={"theme": theme, "action": "set_theme"},
    )


def _cmd_help() -> CommandResponse:
    """Handle /help command."""
    help_text = """Available commands:
Model: /model [name], /updateAllModels <provider>
Thread: /newThread [name], /spawn <ctx>, /spawnAutoApprovedChildThread <ctx>
        /thread <id>, /threads, /parentThread, /listChildren
        /deleteThread, /duplicateThread, /rename <name>
        /waitForThreads <ids>
Tools: /toggleAutoApproval, /toolsOn, /toolsOff, /toolsStatus
       /disableTool <name>, /enableTool <name>, /toolsSecrets <on|off>
Sandbox: /toggleSandboxing, /setSandboxConfiguration <file.json>,
         /getSandboxingConfig
Display: /togglePanel <chat/children/system>, /toggleBorders, /theme <name>
Other: /cost, /schedulers, /enterMode <send/newline>, /paste, /quit, /help

Shell: $ <cmd> (visible), $$ <cmd> (hidden)"""

    return CommandResponse(
        success=True,
        message=help_text,
    )


# --- Thread settings endpoints ---

@app.get("/api/threads/{thread_id}/settings")
async def get_thread_settings(thread_id: str):
    """Get thread settings including auto-approval status."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    return {
        "auto_approval": get_auto_approval_status(thread_id),
        "model_key": current_thread_model(db, thread_id),
    }


@app.get("/api/threads/{thread_id}/state")
async def get_thread_state_endpoint(thread_id: str):
    """Get the current state of a thread (running, waiting, etc.)."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    state = thread_state(db, thread_id)
    root_id = get_thread_root_id(thread_id)

    return {
        "state": state,
        "scheduler_running": root_id in active_schedulers,
    }


@app.get("/api/threads/{thread_id}/sandbox")
async def get_thread_sandbox(thread_id: str):
    """Get sandbox status for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        status = get_thread_sandbox_status(db, thread_id)
        user_control = True
        try:
            user_control = is_user_sandbox_control_enabled(db, thread_id)
        except Exception:
            pass

        return {
            "enabled": status.get("enabled", False),
            "effective": status.get("effective", False),
            "available": status.get("available", False),
            "provider": status.get("provider"),
            "config_source": status.get("config_source"),
            "config_path": status.get("config_path"),
            "warning": status.get("warning"),
            "user_control_enabled": user_control,
        }
    except Exception as e:
        return {
            "enabled": False,
            "effective": False,
            "available": False,
            "error": str(e),
        }


@app.post("/api/threads/{thread_id}/sandbox")
async def set_thread_sandbox(thread_id: str, enabled: bool = True, config_name: Optional[str] = None):
    """Set sandbox configuration for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        if not is_user_sandbox_control_enabled(db, thread_id):
            raise HTTPException(status_code=403, detail="User sandbox control is disabled for this thread")
    except HTTPException:
        raise
    except Exception:
        pass  # Older version, assume enabled

    try:
        if config_name:
            set_thread_sandbox_config(
                db,
                thread_id,
                enabled=enabled,
                config_name=config_name,
                reason='API',
            )
        else:
            cfg = get_thread_sandbox_config(db, thread_id)
            set_thread_sandbox_config(
                db,
                thread_id,
                enabled=enabled,
                settings=cfg.settings,
                reason='API',
            )

        status = get_thread_sandbox_status(db, thread_id)
        return {
            "enabled": status.get("enabled", False),
            "effective": status.get("effective", False),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/threads/{thread_id}/settings/auto-approval")
async def set_auto_approval(thread_id: str, enabled: bool = True):
    """Enable or disable auto-approval for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    current_state = get_auto_approval_status(thread_id)

    # Only emit event if state is changing
    if current_state != enabled:
        decision = "global_approval" if enabled else "revoke_global_approval"
        reason = f"Auto-approval {'enabled' if enabled else 'disabled'} via API"
        approve_tool_calls_for_thread(db, thread_id, decision=decision, reason=reason)

    return {"auto_approval": enabled}


# --- Model endpoints ---

@app.get("/api/models", response_model=ModelsResponse)
async def get_models():
    """Get available models with default."""
    models = []
    for key, config in models_config.items():
        models.append(ModelInfo(
            key=key,
            provider=config.get("provider", "unknown"),
            model_id=config.get("model_name", key),
            display_name=key,  # The key is the display name in eggllm format
        ))
    return ModelsResponse(models=models, default_model=default_model_key)


@app.post("/api/threads/{thread_id}/model")
async def set_model(thread_id: str, request: SetModelRequest):
    """Set the model for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if request.model_key not in models_config:
        raise HTTPException(status_code=400, detail="Invalid model key")

    set_thread_model(db, thread_id, request.model_key)
    return {"status": "ok", "model_key": request.model_key}


# --- Tool call endpoints ---

@app.get("/api/threads/{thread_id}/tools", response_model=List[ToolCallInfo])
async def get_tool_calls(thread_id: str):
    """Get tool calls for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    states = build_tool_call_states(db, thread_id)
    tools = []
    for tc_id, tc in states.items():
        tools.append(ToolCallInfo(
            id=tc_id,
            name=tc.name,
            arguments=tc.arguments,
            state=tc.state,
            output=tc.finished_output,
            approval_decision=tc.approval_decision,
            output_decision=tc.output_decision,
        ))
    return tools


@app.post("/api/threads/{thread_id}/tools/approve")
async def approve_tool(thread_id: str, request: ApprovalRequest):
    """Approve or deny a tool call."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Get current tool states
    states = build_tool_call_states(db, thread_id)
    tc = states.get(request.tool_call_id)

    if not tc:
        raise HTTPException(status_code=404, detail="Tool call not found")

    if tc.state == "TC1":
        # Execution approval - use 'granted' or 'denied'
        decision = "granted" if request.approved else "denied"
        approve_tool_calls_for_thread(
            db,
            thread_id,
            decision=decision,
            tool_call_id=request.tool_call_id,
        )
    elif tc.state == "TC4":
        # Output approval - decision is the output handling: 'whole', 'partial', 'omit'
        # For output approval, we use 'granted' with the output decision
        output_decision = request.output_decision or ("whole" if request.approved else "omit")
        approve_tool_calls_for_thread(
            db,
            thread_id,
            decision=output_decision,
            tool_call_id=request.tool_call_id,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Tool call in state {tc.state} cannot be approved")

    return {"status": "ok"}


# --- Token stats endpoint ---

@app.get("/api/threads/{thread_id}/stats", response_model=ThreadTokenStats)
async def get_token_stats(thread_id: str):
    """Get token statistics for a thread."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Get stats with cost estimates if llm_client is available
    stats = total_token_stats(db, thread_id, llm=llm_client)

    # Extract api_usage - fields are at top level of api_usage dict
    api_usage = stats.get("api_usage", {})
    cost_info = api_usage.get("cost_usd", {}) if isinstance(api_usage.get("cost_usd"), dict) else {}

    input_tokens = api_usage.get("total_input_tokens", 0) or 0
    output_tokens = api_usage.get("total_output_tokens", 0) or 0
    reasoning_tokens = api_usage.get("total_reasoning_tokens", 0) or 0
    cached_tokens = api_usage.get("cached_tokens", 0) or 0

    return ThreadTokenStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        total_tokens=input_tokens + output_tokens + reasoning_tokens,
        cost_usd=cost_info.get("total") if cost_info else None,
        context_tokens=stats.get("context_tokens", 0) or 0,
    )


# --- SSE streaming endpoint ---

@app.get("/api/threads/{thread_id}/events")
async def stream_events(thread_id: str):
    """Stream events for a thread via SSE.

    Starts from the current max event_seq to avoid replaying history.
    Historical messages are fetched via the /messages endpoint.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Get current max event_seq to start from - don't replay historical events
    # This prevents UI freeze when switching to a thread with many events
    try:
        current_max_seq = db.max_event_seq(thread_id)
    except Exception:
        current_max_seq = -1

    async def event_generator():
        watcher = EventWatcher(db, thread_id, after_seq=current_max_seq)
        try:
            async for batch in watcher.aiter():
                for row in batch:
                    event_type = row["type"] if "type" in row.keys() else "unknown"
                    payload = {}
                    if "payload_json" in row.keys() and row["payload_json"]:
                        try:
                            payload = json.loads(row["payload_json"])
                        except:
                            pass

                    event_data = {
                        "event_seq": row["event_seq"],
                        "event_type": event_type,
                        "msg_id": row["msg_id"] if "msg_id" in row.keys() else None,
                        "invoke_id": row["invoke_id"] if "invoke_id" in row.keys() else None,
                        "payload": payload,
                    }

                    yield {
                        "event": event_type,
                        "data": json.dumps(event_data),
                    }
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


# --- WebSocket endpoint ---

class ConnectionManager:
    """Manage WebSocket connections."""

    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, thread_id: str):
        await websocket.accept()
        if thread_id not in self.active_connections:
            self.active_connections[thread_id] = []
        self.active_connections[thread_id].append(websocket)

    def disconnect(self, websocket: WebSocket, thread_id: str):
        if thread_id in self.active_connections:
            self.active_connections[thread_id].remove(websocket)
            if not self.active_connections[thread_id]:
                del self.active_connections[thread_id]

    async def broadcast(self, thread_id: str, message: dict):
        if thread_id in self.active_connections:
            for connection in self.active_connections[thread_id]:
                await connection.send_json(message)


manager = ConnectionManager()


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """WebSocket endpoint for real-time communication."""
    await manager.connect(websocket, thread_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "send_message":
                content = data.get("content", "")
                if content and db:
                    append_message(db, thread_id, role="user", content=content)
                    await manager.broadcast(thread_id, {
                        "type": "message_sent",
                        "thread_id": thread_id,
                    })

            elif msg_type == "approve_tool":
                tc_id = data.get("tool_call_id")
                approved = data.get("approved", False)
                if tc_id and db:
                    states = build_tool_call_states(db, thread_id)
                    tc = states.get(tc_id)
                    if tc and tc.state in ("TC1", "TC4"):
                        decision = "granted" if approved else "denied"
                        if tc.state == "TC4":
                            decision = data.get("output_decision", "whole" if approved else "omit")
                        approve_tool_calls_for_thread(
                            db,
                            thread_id,
                            decision=decision,
                            tool_call_id=tc_id,
                        )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket, thread_id)


# --- Autocomplete endpoint ---

@app.get("/api/autocomplete")
async def get_autocomplete(
    line: str,
    cursor: int = -1,
    thread_id: Optional[str] = None,
):
    """Get autocomplete suggestions for the input line.

    Returns a list of suggestions with:
    - display: text to show in dropdown
    - insert: text to insert at cursor
    - replace: number of chars to delete before inserting (optional)
    - meta: additional info to show (optional)
    """
    if not db:
        return {"suggestions": []}

    if cursor < 0:
        cursor = len(line)

    prefix = line[:cursor]
    suggestions = []

    # Helper to get last token for partial matching
    import re
    def last_token(s: str) -> str:
        m = re.search(r"([\w\-.:/~]+)$", s)
        return m.group(1) if m else ""

    # Command completion
    if prefix.startswith('/'):
        sp = prefix.find(' ')
        if sp == -1:
            # Complete command name - always return full command for robust replacement
            commands = [
                '/help', '/model', '/updateAllModels',
                '/spawn', '/spawnAutoApprovedChildThread', '/newThread',
                '/threads', '/thread', '/parentThread', '/listChildren',
                '/deleteThread', '/duplicateThread', '/rename', '/waitForThreads',
                '/toggleAutoApproval', '/toolsOn', '/toolsOff', '/toolsStatus',
                '/disableTool', '/enableTool', '/toolsSecrets',
                '/toggleSandboxing', '/setSandboxConfiguration', '/getSandboxingConfig',
                '/togglePanel', '/toggleBorders', '/theme',
                '/cost', '/schedulers', '/enterMode', '/paste', '/quit',
            ]
            pref_lower = prefix.lower()
            for cmd in commands:
                if pref_lower in cmd.lower():
                    suggestions.append({
                        "display": cmd,
                        "insert": cmd,  # Full command for replacement
                        "replace": len(prefix),
                    })
        else:
            # Complete command arguments
            cmd = prefix[:sp]
            arg = prefix[sp+1:]
            arg_tok = last_token(arg)

            if cmd == '/model':
                # Model name suggestions - replace entire argument (supports multi-word search)
                # Strip trailing whitespace from arg for matching
                arg_stripped = arg.rstrip()
                if arg_stripped:
                    # Split into words and check if all words are found in the model name
                    words = arg_stripped.lower().split()
                    for key in sorted(models_config.keys()):
                        if all(w in key.lower() for w in words):
                            suggestions.append({
                                "display": key,
                                "insert": key,
                                "replace": len(arg_stripped),  # Replace entire argument
                            })
                else:
                    # No argument - show all models
                    for key in sorted(models_config.keys()):
                        suggestions.append({
                            "display": key,
                            "insert": key,
                            "replace": 0,
                        })

            elif cmd in ('/thread', '/deleteThread', '/waitForThreads'):
                # Thread ID suggestions with rich info like egg.py
                arg_lower = arg_tok.lower()
                threads = list_threads(db)
                # Sort by created_at descending
                try:
                    threads.sort(key=lambda t: t.created_at or '', reverse=True)
                except:
                    pass

                # Get current thread ID for [CUR] indicator
                cur_thread_id = thread_id

                # Check which threads are streaming (have active schedulers)
                streaming_threads = set(active_schedulers.keys())

                # Filter ALL threads first, then limit results
                matched_count = 0
                for t in threads:
                    tid = t.thread_id
                    name = t.name or ''
                    recap = t.short_recap or ''
                    status = t.status or 'unknown'
                    hay = f"{tid} {name} {recap}".lower()
                    if arg_lower and arg_lower not in hay:
                        continue

                    # Build display like egg.py
                    parts = []
                    if tid == cur_thread_id:
                        parts.append("[CUR]")
                    if tid in streaming_threads:
                        parts.append("[STREAM]")
                    parts.append(tid[-8:])

                    # Status indicator
                    if status == 'active':
                        parts.append(f"<{status}>")
                    elif status not in ('waiting_user', 'unknown'):
                        parts.append(f"<{status}>")

                    if recap:
                        parts.append(f"- {recap[:30]}")
                    if name:
                        parts.append(f"({name})")

                    display = " ".join(parts)
                    suggestions.append({
                        "display": display,
                        "insert": tid,
                        "replace": len(arg_tok),
                    })
                    matched_count += 1
                    if matched_count >= 50:  # Limit results after filtering
                        break

            elif cmd == '/setSandboxConfiguration':
                # Suggest sandbox config files from .egg/sandbox/
                import os
                sandbox_dir = Path.cwd() / ".egg" / "sandbox"
                if sandbox_dir.is_dir():
                    try:
                        arg_lower = arg_tok.lower()
                        for f in sorted(sandbox_dir.iterdir()):
                            if f.is_file() and f.suffix == '.json':
                                name = f.name
                                if not arg_lower or arg_lower in name.lower():
                                    suggestions.append({
                                        "display": name,
                                        "insert": name,
                                        "replace": len(arg_tok),
                                    })
                    except Exception:
                        pass

            elif cmd in ('/spawn', '/spawnAutoApprovedChildThread'):
                # Filesystem path suggestions
                import os
                import glob as _glob
                if arg_tok:
                    expanded = os.path.expanduser(arg_tok)
                    base_dir = os.path.dirname(expanded) or '.'
                    needle = os.path.basename(expanded)
                    try:
                        if os.path.isdir(base_dir):
                            entries = os.listdir(base_dir)
                            for name in sorted(entries)[:20]:
                                if needle and not name.lower().startswith(needle.lower()):
                                    continue
                                path = os.path.join(base_dir, name)
                                suffix = '/' if os.path.isdir(path) else ''
                                full_path = path + suffix
                                suggestions.append({
                                    "display": name + suffix,
                                    "insert": full_path,
                                    "replace": len(arg_tok),
                                })
                    except:
                        pass

            elif cmd == '/updateAllModels':
                # Provider name suggestions
                providers = ['openai', 'anthropic', 'google', 'deepseek', 'openrouter', 'xai']
                arg_lower = arg_tok.lower()
                for p in providers:
                    if not arg_lower or arg_lower in p.lower():
                        suggestions.append({
                            "display": p,
                            "insert": p,
                            "replace": len(arg_tok),
                        })

            elif cmd in ('/disableTool', '/enableTool'):
                # Tool name suggestions
                tool_names = ['bash', 'computer', 'text_editor', 'mcp']
                arg_lower = arg_tok.lower()
                for name in tool_names:
                    if not arg_lower or arg_lower in name.lower():
                        suggestions.append({
                            "display": name,
                            "insert": name,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/toolsSecrets':
                # on/off suggestions
                for opt in ['on', 'off']:
                    if not arg_tok or arg_tok.lower() in opt:
                        suggestions.append({
                            "display": opt,
                            "insert": opt,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/theme':
                # Theme name suggestions
                arg_lower = arg_tok.lower()
                for theme in THEMES:
                    if not arg_lower or arg_lower in theme.lower():
                        suggestions.append({
                            "display": theme,
                            "insert": theme,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/waitForThreads':
                # Thread ID suggestions (same as /thread)
                arg_lower = arg_tok.lower()
                threads = list_threads(db)
                for t in threads[:20]:
                    tid = t.thread_id
                    name = t.name or ''
                    hay = f"{tid} {name}".lower()
                    if not arg_lower or arg_lower in hay:
                        display = f"{tid[-8:]}"
                        if name:
                            display += f"  {name}"
                        suggestions.append({
                            "display": display,
                            "insert": tid,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/togglePanel':
                # Panel name suggestions
                for panel in ['chat', 'children', 'system']:
                    if not arg_tok or arg_tok.lower() in panel:
                        suggestions.append({
                            "display": panel,
                            "insert": panel,
                            "replace": len(arg_tok),
                        })

            elif cmd == '/enterMode':
                # Mode suggestions
                for mode in ['send', 'newline']:
                    if not arg_tok or arg_tok.lower() in mode:
                        suggestions.append({
                            "display": mode,
                            "insert": mode,
                            "replace": len(arg_tok),
                        })

    # Shell command completion ($ prefix)
    elif prefix.startswith('$'):
        # Could add shell command suggestions here
        pass

    # Regular text - filesystem paths and conversation words
    elif prefix:
        import os
        tok = last_token(prefix)
        fs_suggestions = []

        # Try filesystem completion first (like egg.py)
        if tok:
            expanded = os.path.expanduser(tok)
            base_dir = expanded
            needle = ''
            if not os.path.isdir(expanded):
                base_dir = os.path.dirname(expanded) or '.'
                needle = os.path.basename(expanded)
            try:
                if os.path.isdir(base_dir):
                    entries = os.listdir(base_dir)
                    for name in sorted(entries):
                        if needle and not name.lower().startswith(needle.lower()):
                            continue
                        path = os.path.join(base_dir, name)
                        suffix = '/' if os.path.isdir(path) else ''
                        full_path = path + suffix
                        fs_suggestions.append({
                            "display": name + suffix,
                            "insert": full_path,
                            "replace": len(tok),
                        })
                        if len(fs_suggestions) >= 20:
                            break
            except:
                pass

        # If filesystem found matches, use those
        if fs_suggestions:
            suggestions.extend(fs_suggestions)
        # Otherwise, fall back to conversation word completion
        elif thread_id and tok and len(tok) >= 2:
            t = db.get_thread(thread_id)
            if t and t.snapshot_json:
                try:
                    import json
                    snap = json.loads(t.snapshot_json)
                    msgs = snap.get('messages', []) or []
                    words = set()
                    tok_lower = tok.lower()
                    for msg in msgs[-100:]:  # Last 100 messages
                        content = msg.get('content') or ''
                        if isinstance(content, str):
                            for word in re.findall(r"[A-Za-z0-9_]{3,}", content):
                                if word.lower().startswith(tok_lower) and word.lower() != tok_lower:
                                    words.add(word)
                    for word in sorted(words)[:15]:
                        suggestions.append({
                            "display": word,
                            "insert": word,
                            "replace": len(tok),
                        })
                except:
                    pass

    return {"suggestions": suggestions[:20]}  # Limit total


# Health check
@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "db_initialized": db is not None,
        "scheduler_running": scheduler is not None,
        "models_loaded": len(models_config) > 0,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
