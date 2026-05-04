from __future__ import annotations

"""Host-side bridge for REPL programmatic tool calls.

The bridge maps an eval token to the runtime thread currently executing REPL
code.  Tool calls from `eggtools` are enqueued as normal RA3 tool calls on that
runtime thread and completion is observed through TC6 events.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .db import ThreadsDB


@dataclass(frozen=True)
class EvalContext:
    token: str
    db_path: str
    caller_thread_id: str
    runtime_thread_id: str
    session_id: Optional[str]
    # Canonicalized eval/programmatic-tool timeout in seconds.
    timeout_sec: Optional[float] = 30.0
    drive_runtime_tools: bool = False
    expires_at: Optional[float] = None


class ReplBridgeError(Exception):
    """Base class for REPL bridge errors."""


class ReplToolTimeout(ReplBridgeError):
    """Raised when a REPL tool call did not reach TC6 before timeout."""

    def __init__(self, thread_id: str, tool_call_id: str, state: str):
        self.thread_id = thread_id
        self.tool_call_id = tool_call_id
        self.state = state
        super().__init__(
            f"REPL tool call {tool_call_id} on thread {thread_id} did not finish (state={state})."
        )


_EVAL_CONTEXTS: Dict[str, EvalContext] = {}


def create_eval_context(
    db: ThreadsDB,
    *,
    caller_thread_id: str,
    runtime_thread_id: str,
    session_id: Optional[str],
    timeout_sec: Optional[float] = 30.0,
    drive_runtime_tools: bool = False,
    ttl_sec: Optional[float] = None,
) -> EvalContext:
    """Create and register an eval context, returning its token-bearing record."""

    token = os.urandom(24).hex()
    expires_at = time.time() + float(ttl_sec) if ttl_sec is not None else None
    ctx = EvalContext(
        token=token,
        db_path=str(Path(db.path)),
        caller_thread_id=caller_thread_id,
        runtime_thread_id=runtime_thread_id,
        session_id=session_id,
        timeout_sec=timeout_sec,
        drive_runtime_tools=drive_runtime_tools,
        expires_at=expires_at,
    )
    _EVAL_CONTEXTS[token] = ctx
    return ctx


def resolve_eval_context(token: str) -> EvalContext:
    """Resolve an eval token or raise ReplBridgeError."""

    ctx = _EVAL_CONTEXTS.get(str(token or ""))
    if ctx is None:
        raise ReplBridgeError("Invalid or expired REPL eval token")
    if ctx.expires_at is not None and time.time() > ctx.expires_at:
        _EVAL_CONTEXTS.pop(ctx.token, None)
        raise ReplBridgeError("Expired REPL eval token")
    return ctx


def dispose_eval_context(token: str) -> None:
    _EVAL_CONTEXTS.pop(str(token or ""), None)


def _authorize(db: ThreadsDB, runtime_thread_id: str, name: str) -> None:
    from .tools_config import get_thread_tools_config

    cfg = get_thread_tools_config(db, runtime_thread_id)
    if not cfg.is_tool_allowed(name):
        raise ReplBridgeError(f"Tool '{name}' is not allowed for runtime thread {runtime_thread_id}")


def _drive_runtime_once(db: ThreadsDB, runtime_thread_id: str) -> bool:
    """Run one runner step for tests/special modes.

    The default bridge contract relies on the active subtree scheduler.  This
    helper is only used when EvalContext.drive_runtime_tools=True.
    """

    from .runner import ThreadRunner
    from .tools import create_default_tools

    runner = ThreadRunner(db, runtime_thread_id, llm=object(), tools=create_default_tools())
    return asyncio.run(runner.run_once())


def call_tool(token: str, name: str, arguments: Optional[Dict[str, Any]] = None, *, timeout_sec: Optional[float] = None) -> str:
    """Call an Egg tool from REPL code through the event-sourced bridge.

    The tool call is represented as a hidden RA3 user tool call on the runtime
    thread.  By default this function waits for the normal subtree scheduler to
    execute it.  Tests/special callers may enable direct runtime driving in the
    EvalContext.
    """

    ctx = resolve_eval_context(token)
    db = ThreadsDB(ctx.db_path)
    tool_name = str(name or "").strip()
    if not tool_name:
        raise ReplBridgeError("Tool name is required")
    args = dict(arguments or {})
    tool_timeout_sec = args.pop("_egg_tool_timeout_sec", None)
    _authorize(db, ctx.runtime_thread_id, tool_name)

    effective_timeout = ctx.timeout_sec if timeout_sec is None else timeout_sec
    if tool_timeout_sec is not None:
        try:
            effective_timeout = float(tool_timeout_sec)
        except Exception:
            pass
    try:
        effective_timeout_value = float(effective_timeout) if effective_timeout is not None else None
    except Exception:
        effective_timeout_value = None
    if effective_timeout_value is not None and effective_timeout_value <= 0:
        effective_timeout_value = None

    # Persist the effective timeout in the queued tool-call arguments so the
    # runtime runner's timeout display, the tool implementation, and this bridge
    # wait all observe the same limit.  This also covers Docker REPL helpers,
    # whose file-RPC envelope carries timeout_sec outside the JSON arguments.
    if effective_timeout_value is not None:
        try:
            current_arg_timeout = float(args.get("timeout_sec")) if args.get("timeout_sec") is not None else None
        except Exception:
            current_arg_timeout = None
        if current_arg_timeout is None or current_arg_timeout <= 0:
            args["timeout_sec"] = effective_timeout_value

    from .api import enqueue_user_tool_call, wait_for_tool_call_result

    try:
        content_args = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:
        content_args = str(args)

    tcid = enqueue_user_tool_call(
        db,
        ctx.runtime_thread_id,
        tool_name,
        args,
        content=f"eggtools.{tool_name}({content_args})",
        hidden=True,
        keep_user_turn=True,
        origin="repl",
        auto_approve=True,
        approval_reason="Approved as REPL programmatic tool call",
    )

    if not ctx.drive_runtime_tools:
        result = wait_for_tool_call_result(
            db,
            ctx.runtime_thread_id,
            tcid,
            timeout_sec=effective_timeout_value,
            poll_interval=0.05,
        )
        if result.timed_out or result.state != "TC6":
            raise ReplToolTimeout(ctx.runtime_thread_id, tcid, result.state)
        return result.content or ""

    start = time.time()
    while True:
        result = wait_for_tool_call_result(
            db,
            ctx.runtime_thread_id,
            tcid,
            timeout_sec=0,
            poll_interval=0.001,
        )
        if not result.timed_out and result.state == "TC6":
            return result.content or ""
        if effective_timeout_value is not None and (time.time() - start) >= effective_timeout_value:
            raise ReplToolTimeout(ctx.runtime_thread_id, tcid, result.state)
        _drive_runtime_once(db, ctx.runtime_thread_id)
        time.sleep(0.01)


__all__ = [
    "EvalContext",
    "ReplBridgeError",
    "ReplToolTimeout",
    "create_eval_context",
    "resolve_eval_context",
    "dispose_eval_context",
    "call_tool",
]
