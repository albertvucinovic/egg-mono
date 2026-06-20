"""Message API routes for eggw backend."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from eggthreads import (
    COMPACTION_EVENT_TYPE,
    SnapshotBuilder,
    ThreadsDB,
    append_message,
    build_tool_call_states,
    create_snapshot,
    get_active_get_user_message_waiting_note,
    interrupt_thread,
)
from eggthreads.attachment_staging import safe_display_filename, save_attachment_bytes_for_thread
from eggthreads.content_parts import content_to_plain_text
from eggthreads.image_generation import (
    generate_openai_image_artifacts,
    image_generation_result_content_parts,
    normalize_image_generation_model_key,
    normalize_openai_image_generation_options,
)
from eggthreads.provider_output_artifacts import (
    ProviderOutputArtifactAccessError,
    ProviderOutputArtifactError,
    ProviderOutputArtifactNotFoundError,
    resolve_provider_output_bytes,
)
from eggllm.image_generation import ImageGenerationConfigError, ImageGenerationError, ImageGenerationProviderError

from ..models import (
    AttachmentUploadResponse,
    ImageGenerationRequest,
    ImageGenerationResponse,
    MessageContent,
    SendMessageRequest,
)
from .. import core
from ..core import ensure_scheduler_for, get_thread_root_id

router = APIRouter(prefix="/api/threads", tags=["messages"])

GET_USER_MESSAGE_TOOL_NAME = "get_user_message_while_preserving_llm_turn"
GET_USER_INTERRUPT_CONTENT = "User interrupted get_user_message_while_preserving_llm_turn."


def _attachment_workspace() -> Path:
    """Return the workspace root for EggW input artifacts."""

    try:
        db_path = Path(core.db.path).resolve()  # type: ignore[union-attr]
        if db_path.parent.name == ".egg":
            return db_path.parent.parent
    except Exception:
        pass
    return Path.cwd().resolve()


def _content_disposition(disposition: str, filename: str) -> str:
    safe_name = safe_display_filename(filename, default="artifact")
    ascii_name = "".join(ch if 0x20 <= ord(ch) < 0x7F and ch not in {'"', '\\'} else "_" for ch in safe_name)
    ascii_name = ascii_name or "artifact"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe_name)}"


def _image_generation_options(request: ImageGenerationRequest) -> dict[str, object]:
    """Return explicit provider image-generation options from an API request."""

    try:
        return normalize_openai_image_generation_options(request.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


def _image_generation_model_key(request: ImageGenerationRequest) -> str | None:
    """Resolve the model/backend alias pair accepted by the EggW API."""

    try:
        return normalize_image_generation_model_key(request.model, request.backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


def _cancel_active_get_user_wait(thread_id: str, waiting_note: dict | None) -> bool:
    """Publish the terminal-equivalent interrupted result for an active get-user wait."""
    if not core.db or not isinstance(waiting_note, dict):
        return False
    tool_call_id = str(waiting_note.get("tool_call_id") or "")
    if not tool_call_id:
        return False

    try:
        tc = build_tool_call_states(core.db, thread_id).get(tool_call_id)
    except Exception:
        tc = None
    if tc is None or getattr(tc, "published", False):
        return False
    name = str(getattr(tc, "name", "") or tool_call_id)
    if name != GET_USER_MESSAGE_TOOL_NAME:
        return False

    core.db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_="tool_call.output_approval",
        msg_id=None,
        invoke_id=None,
        payload={
            "tool_call_id": tool_call_id,
            "decision": "whole",
            "reason": "Cancelled by user via web interrupt",
            "preview": GET_USER_INTERRUPT_CONTENT,
        },
    )

    if getattr(tc, "parent_role", None) == "assistant":
        core.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="msg.create",
            msg_id=os.urandom(10).hex(),
            invoke_id=None,
            payload={
                "role": "tool",
                "content": GET_USER_INTERRUPT_CONTENT,
                "tool_call_id": tool_call_id,
                "name": name,
                "keep_user_turn": True,
            },
        )
        create_snapshot(core.db, thread_id)

    return True


def _compaction_marker_message(marker: dict, fallback_start_seq: int) -> MessageContent:
    marker_id = f"compaction-{marker.get('marker_event_seq') or fallback_start_seq}"
    start_msg_id = str(marker.get("start_msg_id") or "")
    start_short = start_msg_id[-8:] if start_msg_id else "unknown"
    detail_parts = []
    if marker.get("marker_event_seq") is not None:
        detail_parts.append(f"marker #{marker.get('marker_event_seq')}")
    if marker.get("start_event_seq") is not None:
        detail_parts.append(f"start event #{marker.get('start_event_seq')}")
    if marker.get("selector"):
        detail_parts.append(f"selector {marker.get('selector')}")
    if marker.get("created_by"):
        detail_parts.append(f"by {marker.get('created_by')}")
    details = f" ({'; '.join(detail_parts)})" if detail_parts else ""
    return MessageContent(
        id=marker_id,
        role="compaction_marker",
        kind="compaction_marker",
        content=(
            f"Compaction boundary: API context now starts at msg_{start_short}{details}. "
            "Earlier messages remain visible in the UI/raw history."
        ),
        start_msg_id=start_msg_id or None,
        start_event_seq=marker.get("start_event_seq"),
        marker_event_seq=marker.get("marker_event_seq"),
        selector=marker.get("selector"),
        created_by=marker.get("created_by"),
    )


def _get_messages_sync(db_path: str, thread_id: str) -> List[MessageContent]:
    """Synchronous helper to fetch messages - runs in thread pool to avoid blocking event loop."""
    # Use fresh connection to ensure we see latest writes from other processes
    fresh_db = ThreadsDB(db_path)
    t = fresh_db.get_thread(thread_id)
    if not t:
        return None  # Signal thread not found

    # Build fresh snapshot from ALL events (not cached snapshot_json)
    cur = fresh_db.conn.execute(
        "SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (thread_id,)
    )
    events = cur.fetchall()

    builder = SnapshotBuilder()
    snap = builder.build(events)

    # Get per-message token stats from cached snapshot (if available)
    token_stats = {}
    per_message_tokens = {}
    if t.snapshot_json:
        try:
            cached_snap = json.loads(t.snapshot_json)
            token_stats = cached_snap.get("token_stats", {})
            per_message_tokens = token_stats.get("per_message", {}) if isinstance(token_stats, dict) else {}
        except:
            pass

    raw_compactions = []
    for row in events:
        try:
            typ = row["type"]
        except Exception:
            typ = row[2] if len(row) > 2 else None
        if typ != COMPACTION_EVENT_TYPE:
            continue
        try:
            payload_json = row["payload_json"]
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            marker_event_seq = int(row["event_seq"])
        except Exception:
            marker_event_seq = None
        try:
            start_event_seq = int(payload.get("start_event_seq"))
        except Exception:
            start_event_seq = None
        raw_compactions.append({
            "marker_event_seq": marker_event_seq,
            "start_event_seq": start_event_seq,
            "start_msg_id": payload.get("start_msg_id"),
            "selector": payload.get("selector"),
            "created_by": payload.get("created_by"),
        })

    markers_by_start_seq = {}
    for marker in raw_compactions:
        start_seq = marker.get("start_event_seq")
        if isinstance(start_seq, int):
            markers_by_start_seq.setdefault(start_seq, []).append(marker)

    messages = []
    for msg in snap.get("messages", []):
        msg_id = msg.get("msg_id", "")

        try:
            msg_event_seq = int(msg.get("event_seq"))
        except Exception:
            msg_event_seq = None
        if msg_event_seq is not None:
            for marker in markers_by_start_seq.get(msg_event_seq, []):
                messages.append(_compaction_marker_message(marker, msg_event_seq))

        # Get per-message token count from cached stats
        pm_info = per_message_tokens.get(msg_id, {}) if msg_id else {}
        total_tokens = None
        if pm_info:
            content_tok = int(pm_info.get("content_tokens", 0) or 0)
            reasoning_tok = int(pm_info.get("reasoning_tokens", 0) or 0)
            tool_calls_tok = int(pm_info.get("tool_calls_tokens", 0) or 0)
            total_tokens = pm_info.get("total_tokens") or (content_tok + reasoning_tok + tool_calls_tok)
            if total_tokens:
                total_tokens = int(total_tokens)

        # Parse timestamp
        ts_raw = msg.get("ts")
        timestamp = None
        if ts_raw:
            try:
                timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                try:
                    timestamp = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
                except Exception:
                    pass

        messages.append(MessageContent(
            id=msg_id,
            role=msg.get("role", ""),
            content=msg.get("content"),
            content_text=content_to_plain_text(msg.get("content")),
            reasoning=msg.get("reasoning"),
            tool_calls=msg.get("tool_calls"),
            tool_stream=msg.get("tool_stream") if isinstance(msg.get("tool_stream"), dict) else None,
            tool_calls_stream=msg.get("tool_calls_stream") if isinstance(msg.get("tool_calls_stream"), dict) else None,
            tool_call_id=msg.get("tool_call_id"),
            name=msg.get("name"),
            model_key=msg.get("model_key"),
            timestamp=timestamp,
            tokens=total_tokens,
            tps=float(msg.get("tps")) if isinstance(msg.get("tps"), (int, float)) and float(msg.get("tps")) > 0 else None,
            answer_user_preserve_turn=bool(msg.get("answer_user_preserve_turn")),
            recovery_notice=bool(msg.get("recovery_notice")),
        ))

    return messages


@router.get("/{thread_id}/messages", response_model=List[MessageContent])
async def get_messages(thread_id: str):
    """Get messages for a thread by building fresh snapshot from events.

    This ensures we see all messages including those written by other processes
    (e.g., TUI) that haven't been persisted to snapshot_json yet.

    Runs database operations in thread pool to avoid blocking the async event loop,
    allowing multiple tabs to fetch messages simultaneously.
    """
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # Run database-heavy work in thread pool to avoid blocking event loop
    loop = asyncio.get_running_loop()
    messages = await loop.run_in_executor(None, _get_messages_sync, core.db.path, thread_id)

    if messages is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    return messages


@router.post("/{thread_id}/messages")
async def send_message(thread_id: str, request: SendMessageRequest):
    """Send a message to a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Append user message
    msg_id = append_message(core.db, thread_id, role="user", content=request.content)

    # Ensure scheduler is running for this thread's root
    ensure_scheduler_for(thread_id)

    return {"status": "sent", "message_id": msg_id}


@router.post("/{thread_id}/attachments", response_model=AttachmentUploadResponse)
async def upload_attachment(thread_id: str, file: UploadFile = File(...)):
    """Ingest a browser-uploaded file as a durable thread input attachment."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    raw_filename = str(file.filename or "").strip()
    if not raw_filename:
        raise HTTPException(status_code=400, detail="Uploaded file name is required")

    data = await file.read()
    await file.close()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    display_name = safe_display_filename(raw_filename)
    provenance = {"kind": "eggw_upload"}
    if isinstance(file.content_type, str) and file.content_type.strip():
        provenance["client_content_type"] = file.content_type.strip().lower()

    saved, content_part = save_attachment_bytes_for_thread(
        _attachment_workspace(),
        thread_id,
        data,
        filename=display_name,
        provenance=provenance,
    )

    return AttachmentUploadResponse(
        input_id=saved.input_id,
        metadata=saved.metadata,
        content_part=content_part,
        content_text=content_to_plain_text([content_part], validate=True),
    )


@router.post("/{thread_id}/image-generation", response_model=ImageGenerationResponse)
async def generate_thread_image(thread_id: str, request: ImageGenerationRequest):
    """Generate provider-backed image artifacts and append an assistant message.

    This is the EggW API surface for the same shared image-generation service
    used by the terminal ``/imageGenerate`` command.  It stores generated bytes
    as provider-output artifacts and appends only durable artifact references to
    the transcript.
    """
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    prompt = request.prompt.strip() if isinstance(request.prompt, str) else ""
    if not prompt:
        raise HTTPException(status_code=400, detail="Image generation prompt is required")

    options = _image_generation_options(request)
    model_key = _image_generation_model_key(request)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: generate_openai_image_artifacts(
                _attachment_workspace(),
                thread_id,
                prompt,
                model_key=model_key,
                models_path=core.MODELS_PATH,
                all_models_path=core.ALL_MODELS_PATH,
                options=options,
            ),
        )
    except ImageGenerationConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ImageGenerationProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None
    except ImageGenerationError as e:
        raise HTTPException(status_code=502, detail=str(e)) from None

    content_parts = image_generation_result_content_parts(result)
    message_id = append_message(core.db, thread_id, role="assistant", content=content_parts)
    create_snapshot(core.db, thread_id)

    return ImageGenerationResponse(
        message_id=message_id,
        prompt=result.prompt,
        model_key=result.model_key,
        provider_name=result.provider_name,
        model_name=result.model_name,
        metadata=result.metadata,
        content_parts=content_parts,
        content_text=content_to_plain_text(content_parts, validate=True),
        response_metadata=result.response_metadata,
    )


@router.get("/{thread_id}/provider-output/{artifact_id}")
async def get_provider_output_artifact(
    thread_id: str,
    artifact_id: str,
    descendant_thread_id: str | None = None,
    download: bool = False,
):
    """Return provider-output artifact bytes after thread access checks."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        metadata, data = resolve_provider_output_bytes(
            _attachment_workspace(),
            core.db,
            thread_id,
            artifact_id,
            descendant_thread_id=descendant_thread_id,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid provider output artifact id") from None
    except ProviderOutputArtifactAccessError:
        raise HTTPException(status_code=403, detail="Access denied for provider output artifact") from None
    except ProviderOutputArtifactNotFoundError:
        raise HTTPException(status_code=404, detail="Provider output artifact not found") from None
    except ProviderOutputArtifactError:
        raise HTTPException(status_code=400, detail="Provider output artifact is invalid") from None

    media_type = str(metadata.get("mime_type") or "application/octet-stream").strip().lower() or "application/octet-stream"
    filename = str(metadata.get("filename") or metadata.get("artifact_id") or "artifact")
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(disposition, filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{thread_id}/open")
async def open_thread(thread_id: str):
    """Open a thread for viewing. Ensures scheduler for this thread's root is running."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Ensure scheduler is running for this thread's root (don't stop other schedulers)
    # This allows multiple tabs to view different thread trees simultaneously
    from ..core import start_scheduler
    root_id = get_thread_root_id(thread_id)
    scheduler_running = root_id in core.active_schedulers
    if not scheduler_running:
        start_scheduler(root_id)

    return {
        "status": "ok",
        "thread_id": thread_id,
        "root_id": root_id,
        "scheduler_running": True,
    }


@router.post("/{thread_id}/interrupt")
async def interrupt_thread_endpoint(thread_id: str):
    """Interrupt/cancel current streaming or pending work (Ctrl+C equivalent)."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    get_user_waiting_note = get_active_get_user_message_waiting_note(core.db, thread_id)

    # Interrupt the thread
    result = interrupt_thread(core.db, thread_id, reason="user")

    get_user_cancelled = _cancel_active_get_user_wait(thread_id, get_user_waiting_note)

    # Auto-approve output for any interrupted tool calls so they get published
    # and don't block further interaction. The runner will add an "interrupted" note.
    # We need a brief delay to let the interrupt propagate and tool calls reach TC4.
    await asyncio.sleep(0.1)

    states = build_tool_call_states(core.db, thread_id)
    for tc in states.values():
        if tc.state == "TC4" and tc.finished_reason == "interrupted":
            # Emit output approval with 'whole' decision - runner handles interrupted specially
            full_output = tc.finished_output or ""
            if not isinstance(full_output, str):
                full_output = str(full_output)
            line_count = len(full_output.splitlines()) if full_output else 0
            char_count = len(full_output)

            core.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=thread_id,
                type_='tool_call.output_approval',
                msg_id=None,
                invoke_id=None,
                payload={
                    'tool_call_id': tc.tool_call_id,
                    'decision': 'whole',
                    'reason': 'Auto-approved after interrupt',
                    'preview': full_output,
                    'line_count': line_count,
                    'char_count': char_count,
                },
            )

    return {
        "status": "interrupted",
        "thread_id": thread_id,
        "invoke_id": result,
        "get_user_cancelled": get_user_cancelled,
    }
