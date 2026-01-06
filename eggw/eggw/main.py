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

    # Initialize database
    db = ThreadsDB()
    db.init_schema()  # Create tables if they don't exist

    # Load models
    models_config, default_model_key = load_models_config()

    # Initialize LLM client
    models_path = PROJECT_ROOT / "egg" / "models.json"
    try:
        from eggllm import LLMClient
        llm_client = LLMClient(models_path=models_path)
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
    sched = entry.get("scheduler")
    task = entry.get("task")

    if sched:
        sched.stop()
    if task:
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
    """List all threads."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    threads = []
    for t in list_threads(db):
        children = list_children_ids(db, t.thread_id)
        threads.append(ThreadInfo(
            id=t.thread_id,
            name=t.name,
            parent_id=get_parent(db, t.thread_id),
            model_key=current_thread_model(db, t.thread_id),
            has_children=len(children) > 0,
        ))
    return threads


@app.get("/api/threads/roots", response_model=List[ThreadInfo])
async def get_root_threads():
    """List only root threads."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    threads = []
    # list_root_threads returns thread IDs (strings), not ThreadRow objects
    for thread_id in list_root_threads(db):
        t = db.get_thread(thread_id)
        if not t:
            continue
        children = list_children_ids(db, thread_id)
        threads.append(ThreadInfo(
            id=thread_id,
            name=t.name,
            parent_id=None,
            model_key=current_thread_model(db, thread_id),
            has_children=len(children) > 0,
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
    """Open a thread - starts its scheduler if not already running."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Ensure scheduler is running for this thread's root
    ensure_scheduler_for(thread_id)

    root_id = get_thread_root_id(thread_id)
    return {
        "status": "ok",
        "thread_id": thread_id,
        "root_id": root_id,
        "scheduler_running": root_id in active_schedulers,
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

    import os as _os

    # Create tool call entry
    tc_id = _os.urandom(8).hex()
    tool_call = {
        'id': tc_id,
        'type': 'function',
        'function': {
            'name': 'bash',
            'arguments': json.dumps({'script': script}, ensure_ascii=False),
        },
    }

    extra = {
        'tool_calls': [tool_call],
        'keep_user_turn': True,
        'user_command_type': '$$' if hidden else '$',
    }
    if hidden:
        extra['no_api'] = True

    # Store the user message with tool call
    prefix = '$$ ' if hidden else '$ '
    msg_id = append_message(db, thread_id, 'user', f"{prefix}{script}", extra=extra)

    # Auto-approve the tool call so it executes immediately
    approve_tool_calls_for_thread(
        db,
        thread_id,
        exec_approval='auto',
        output_approval='omit' if hidden else 'whole',
        filter_tc_ids=[tc_id],
    )

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


async def _cmd_toggle_auto_approval(thread_id: str) -> CommandResponse:
    """Handle /toggleAutoApproval command."""
    # TODO: Implement auto-approval toggle in thread settings
    return CommandResponse(
        success=False,
        message="Auto-approval toggle not yet implemented in web UI",
    )


def _cmd_help() -> CommandResponse:
    """Handle /help command."""
    help_text = """Available commands:
/model [name] - Show or set the model
/spawn <context> - Spawn a child thread with context
/spawnChildThread <context> - Same as /spawn
/newThread [name] - Create a new root thread
/toggleAutoApproval - Toggle auto-approval (not yet implemented)
/help - Show this help

Shell commands:
$ <command> - Execute shell command (output visible to model)
$$ <command> - Execute shell command (output hidden from model)"""

    return CommandResponse(
        success=True,
        message=help_text,
    )


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
        # Execution approval
        approve_tool_calls_for_thread(
            db,
            thread_id,
            tool_call_ids=[request.tool_call_id],
            decision="granted" if request.approved else "denied",
        )
    elif tc.state == "TC4":
        # Output approval
        decision = request.output_decision or ("whole" if request.approved else "omit")
        approve_tool_calls_for_thread(
            db,
            thread_id,
            tool_call_ids=[request.tool_call_id],
            decision=decision,
            is_output_approval=True,
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

    stats = total_token_stats(db, thread_id)
    return ThreadTokenStats(
        input_tokens=stats.get("input_tokens", 0),
        output_tokens=stats.get("output_tokens", 0),
        reasoning_tokens=stats.get("reasoning_tokens", 0),
        total_tokens=stats.get("total_tokens", 0),
    )


# --- SSE streaming endpoint ---

@app.get("/api/threads/{thread_id}/events")
async def stream_events(thread_id: str):
    """Stream events for a thread via SSE."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    async def event_generator():
        watcher = EventWatcher(db, thread_id)
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
                            tool_call_ids=[tc_id],
                            decision=decision,
                            is_output_approval=(tc.state == "TC4"),
                        )

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket, thread_id)


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
