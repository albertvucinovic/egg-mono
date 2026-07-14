"""Pydantic models for the eggw API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field


class ThreadInfo(BaseModel):
    """Thread information."""
    id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None
    model_key: Optional[str] = None
    created_at: Optional[datetime] = None
    has_children: bool = False


class CreateThreadResponse(ThreadInfo):
    """New thread plus optional one-shot launcher composer state."""

    initial_draft: Optional[str] = None
    initial_attachment: Optional[Dict[str, Any]] = None
    initial_error: Optional[str] = None


class MessageContent(BaseModel):
    """A single message or transcript marker in a thread."""
    id: str
    role: str  # "user" | "assistant" | "system" | "tool" | "compaction_marker"
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    content_text: Optional[str] = None
    kind: str = "message"
    start_msg_id: Optional[str] = None
    start_event_seq: Optional[int] = None
    marker_event_seq: Optional[int] = None
    selector: Optional[str] = None
    created_by: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_stream: Optional[Dict[str, Any]] = None
    tool_calls_stream: Optional[Dict[str, Any]] = None
    tool_call_id: Optional[str] = None
    output_optimizer: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    timestamp: Optional[datetime] = None
    tokens: Optional[int] = None
    tps: Optional[float] = None
    model_key: Optional[str] = None
    answer_user_preserve_turn: bool = False
    recovery_notice: bool = False


class MessageSnapshotResponse(BaseModel):
    """Paginated transcript plus the exact event cursor it represents."""

    items: List[MessageContent]
    snapshot_cursor: int
    next_before: Optional[str] = None


class ToolCallInfo(BaseModel):
    """Tool call state information."""
    id: str
    name: str
    arguments: Any
    state: str  # TC1, TC2.1, TC2.2, TC3, TC4, TC5, TC6
    output: Optional[str] = None
    approval_decision: Optional[str] = None
    output_decision: Optional[str] = None
    summary: Optional[str] = None


class SendMessageRequest(BaseModel):
    """Request to send a message to a thread."""
    content: Union[str, List[Dict[str, Any]]]


class AttachmentUploadResponse(BaseModel):
    """Response for an uploaded thread input attachment."""
    input_id: str
    metadata: Dict[str, Any]
    content_part: Dict[str, Any]
    content_text: str


class ImageGenerationRequest(BaseModel):
    """Request to generate provider-backed image artifacts for a thread."""

    prompt: Optional[str] = None
    model: Optional[str] = None
    backend: Optional[str] = None
    n: Optional[int] = None
    size: Optional[str] = None
    quality: Optional[str] = None
    output_format: Optional[str] = None
    background: Optional[str] = None


class ImageGenerationResponse(BaseModel):
    """Response for a thread-scoped image generation request."""

    message_id: str
    prompt: str
    model_key: str
    provider_name: str
    model_name: str
    metadata: List[Dict[str, Any]]
    content_parts: List[Dict[str, Any]]
    content_text: str
    response_metadata: Dict[str, Any] = Field(default_factory=dict)


class CommandRequest(BaseModel):
    """Request to execute a command."""
    command: str  # The full command string (e.g., "/model GPT 5" or "$ ls -la")
    staged_attachments: Optional[List[Dict[str, Any]]] = None


class CommandResponse(BaseModel):
    """Response from command execution."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None


class CommandLifecycleResponse(BaseModel):
    """Response from command execution with command lifecycle timing metadata."""

    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    command_id: str
    command_name: str
    started_at: datetime
    finished_at: datetime
    elapsed_sec: float


class EditAnswerDraftRequest(BaseModel):
    """Request to prepare a quoted assistant-answer draft."""

    selector: Optional[str] = None
    source_msg_id: Optional[str] = None


class EditAnswerDraftResponse(BaseModel):
    """Prepared edit-answer draft metadata for the browser editor modal."""

    action: Literal["open_edit_answer_modal"] = "open_edit_answer_modal"
    draft: str
    source_msg_id: str
    source_kind: Literal["assistant_answer", "assistant_note", "input_message", "message"]
    source_suffix: str = ""
    source_label: str = ""
    suppress_transcript: bool = True
    message: Optional[str] = None


class CreateThreadRequest(BaseModel):
    """Request to create a new thread."""
    name: Optional[str] = None
    parent_id: Optional[str] = None
    model_key: Optional[str] = None
    context: Optional[str] = None  # For child threads, context to include
    claim_quick_start: bool = False


class SetModelRequest(BaseModel):
    """Request to set a thread's model."""
    model_key: str


class ApprovalRequest(BaseModel):
    """Request to approve or deny a tool call."""
    tool_call_id: str
    approved: bool
    output_decision: Optional[str] = None  # "whole" | "partial" | "omit"
    decision: Optional[str] = None  # Special decisions like "all-in-turn"


class ThreadTokenStats(BaseModel):
    """Token statistics for a thread."""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    context_tokens: int = 0
    full_thread_tokens: int = 0
    streaming_tps: Optional[float] = None
    current_provider_context_tokens: int = 0
    full_thread_context_tokens: int = 0
    compacted_away_tokens: int = 0
    cached_input_tokens: int = 0
    cached_tokens_last: int = 0
    cached_input_hit_rate: float = 0.0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    approx_call_count: int = 0
    actual_call_count: int = 0
    estimated_call_count: int = 0
    cost_total_usd: Optional[float] = None
    cost_usd_details: Optional[Dict[str, Any]] = None
    cost_warnings: List[str] = []
    api_confirmed_usage: Optional[Dict[str, Any]] = None
    api_usage: Optional[Dict[str, Any]] = None
    api_usage_since_compaction: Optional[Dict[str, Any]] = None
    by_model: Optional[Dict[str, Any]] = None


class ModelInfo(BaseModel):
    """Model information."""
    key: str
    provider: str
    model_id: str
    display_name: Optional[str] = None


class ModelsResponse(BaseModel):
    """Response with models list and default."""
    models: List[ModelInfo]
    default_model: Optional[str] = None


# WebSocket message types
class WSMessage(BaseModel):
    """WebSocket message wrapper."""
    type: str
    data: Dict[str, Any]


# SSE event types
class SSEEvent(BaseModel):
    """Server-Sent Event data."""
    event: str
    data: Dict[str, Any]
