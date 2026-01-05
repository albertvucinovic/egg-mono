"""Pydantic models for the eggw API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class ThreadInfo(BaseModel):
    """Thread information."""
    id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None
    model_key: Optional[str] = None
    created_at: Optional[datetime] = None
    has_children: bool = False


class MessageContent(BaseModel):
    """A single message in a thread."""
    id: str
    role: str  # "user" | "assistant" | "system" | "tool"
    content: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    tokens: Optional[int] = None
    model_key: Optional[str] = None


class ToolCallInfo(BaseModel):
    """Tool call state information."""
    id: str
    name: str
    arguments: Any
    state: str  # TC1, TC2.1, TC2.2, TC3, TC4, TC5, TC6
    output: Optional[str] = None
    approval_decision: Optional[str] = None
    output_decision: Optional[str] = None


class SendMessageRequest(BaseModel):
    """Request to send a message to a thread."""
    content: str


class CreateThreadRequest(BaseModel):
    """Request to create a new thread."""
    name: Optional[str] = None
    parent_id: Optional[str] = None
    model_key: Optional[str] = None
    context: Optional[str] = None  # For child threads, context to include


class SetModelRequest(BaseModel):
    """Request to set a thread's model."""
    model_key: str


class ApprovalRequest(BaseModel):
    """Request to approve or deny a tool call."""
    tool_call_id: str
    approved: bool
    output_decision: Optional[str] = None  # "whole" | "partial" | "omit"


class ThreadTokenStats(BaseModel):
    """Token statistics for a thread."""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


class ModelInfo(BaseModel):
    """Model information."""
    key: str
    provider: str
    model_id: str
    display_name: Optional[str] = None


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
