from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from pathlib import Path
try:
    from eggllm import LLMClient
except Exception:
    LLMClient = None  # type: ignore
from .db import ThreadsDB
from .tools import ToolExecutionResult, ToolRegistry, ToolStreamContext, create_default_tools
from .tool_state import (
    ToolCallState,
    RunnerActionable,
    discover_runner_actionable,
    discover_runner_actionable_cached,
    thread_state,
    build_tool_call_states,
)
from .tools_config import get_thread_tools_config
from .tool_call_id import normalize_tool_call_id
from .terminal_safety import sanitize_terminal_text


ANSWER_USER_PRESERVE_TURN_TOOL_NAME = "answer_user_while_preserving_llm_turn"


# Use SQLite-compatible ISO format without 'T' to allow lexical comparisons in SQL queries
ISO = "%Y-%m-%d %H:%M:%S"


def _utcnow() -> datetime:
    """Timezone-aware UTC now with naive formatting compatibility."""
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime(ISO)


class ContextLimitExceeded(Exception):
    """Raised when a thread's context exceeds the configured limit."""
    pass


def _tool_call_name(tc: Any) -> str:
    """Return the function/tool name from an OpenAI-style tool_call dict."""

    if not isinstance(tc, dict):
        return ""
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name = fn.get("name") or tc.get("name") or ""
    return str(name) if name is not None else ""


def _provider_tool_calls(tool_calls: Any) -> list[Any]:
    """Return tool calls that should remain in provider/API context.

    The interim-answer tool publishes a local-only assistant note and a
    ``no_api`` tool result. Dropping that call from the provider view prevents
    an assistant(tool_calls) turn whose required tool response is intentionally
    hidden from blocking later LLM calls.
    """

    if not isinstance(tool_calls, list):
        return []
    return [tc for tc in tool_calls if _tool_call_name(tc) != ANSWER_USER_PRESERVE_TURN_TOOL_NAME]


def _is_context_length_exceeded_error(exc: BaseException) -> bool:
    """Return True when a provider error is recognizably context-window overflow."""

    if isinstance(exc, ContextLimitExceeded):
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "context_length_exceeded" in text
        or "context length" in text
        or "context window" in text
        or "maximum context" in text
        or "prompt is too long" in text
        or "too many tokens" in text
        or ("input token count" in text and "exceed" in text)
        or ("token" in text and "limit" in text and "exceed" in text)
        or ("token" in text and "maximum" in text and "exceed" in text)
    )


def _now_plus(ttl_sec: int) -> str:
    return (_utcnow() + timedelta(seconds=ttl_sec)).strftime(ISO)


def runner_actionable_resource_class(ra: Optional[RunnerActionable]) -> str:
    """Return scheduler resource class for a RunnerActionable.

    RA1 performs LLM/provider work and consumes the scarce LLM slot pool.
    RA2/RA3 are tool work: they are still real running thread work with a
    lease, but they do not consume LLM concurrency slots.
    """

    return "llm" if ra is not None and ra.kind == "RA1_llm" else "tool"


@dataclass(frozen=True)
class _SchedulerThreadSettings:
    priority: Any = 0
    threshold: Any = None


# Thresholds above which a tool output is considered "long" and should
# be stashed to disk with a preview sent to the LLM instead of the full
# content. Matches the existing "prompt the user" thresholds so
# behaviour is continuous with prior versions.
LONG_OUTPUT_LINE_THRESHOLD = 800
LONG_OUTPUT_CHAR_THRESHOLD = 100_000

# Size of the preview that gets embedded in the tool message when
# the full output is stashed to disk.
PREVIEW_MAX_LINES = 200
PREVIEW_MAX_CHARS = 8000
TOOL_STREAM_PREVIEW_MAX_LINES = PREVIEW_MAX_LINES
TOOL_STREAM_PREVIEW_MAX_CHARS = PREVIEW_MAX_CHARS


class ToolStreamPreviewLimiter:
    """Bound live tool-output streaming to a short preview.

    Full tool output is still accumulated by the runner and later handled by
    the normal output-approval/stash path. This helper only controls what is
    emitted as ``stream.delta`` events for the live UI, so a huge stdout/stderr
    burst does not flood the TUI or event log.
    """

    def __init__(
        self,
        *,
        max_lines: int = TOOL_STREAM_PREVIEW_MAX_LINES,
        max_chars: int = TOOL_STREAM_PREVIEW_MAX_CHARS,
    ):
        self.max_lines = max(0, int(max_lines))
        self.max_chars = max(0, int(max_chars))
        self.chars_seen = 0
        self.lines_seen = 0
        self.suppressed = False

    def filter(self, text: str) -> tuple[str, bool]:
        """Return ``(preview_text, just_suppressed)`` for the next chunk."""
        if not isinstance(text, str) or not text:
            return "", False
        if self.suppressed:
            return "", False

        remaining_chars = self.max_chars - self.chars_seen
        remaining_lines = self.max_lines - self.lines_seen
        if remaining_chars <= 0 or remaining_lines <= 0:
            self.suppressed = True
            return "", True

        out: list[str] = []
        chars_left = remaining_chars
        lines_left = remaining_lines
        i = 0
        n = len(text)
        while i < n and chars_left > 0 and lines_left > 0:
            ch = text[i]
            out.append(ch)
            chars_left -= 1
            self.chars_seen += 1
            i += 1
            if ch == "\n":
                lines_left -= 1
                self.lines_seen += 1

        if i < n:
            self.suppressed = True
            return "".join(out), True
        return "".join(out), False

    def should_emit_indicator(self, chunk_index: int) -> bool:
        """Return True occasionally while output remains suppressed."""
        return self.suppressed and int(chunk_index) % 20 == 0


def tool_stream_suppressed_notice(tool_name: str = "") -> str:
    name = f" for {tool_name}" if tool_name else ""
    return (
        f"\n[Live tool output{name} exceeded preview limit; continuing without streaming. "
        "Full output will be saved to a file if it exceeds the normal long-output threshold.]\n"
    )


def stash_tool_output_and_build_preview(
    db,
    thread_id: str,
    tool_call_id: str,
    full_output: str,
    *,
    max_lines: int = PREVIEW_MAX_LINES,
    max_chars: int = PREVIEW_MAX_CHARS,
) -> tuple:
    """Persist *full_output* to disk and return ``(preview, saved_path)``.

    The file is created under a thread-scoped directory inside the thread's
    working directory (``<thread_wd>/.egg_outputs/<thread_id>/``).  The
    per-thread level is important when parent/child threads intentionally share
    a working directory: a child should be able to read its own long-output
    files, but not parent/sibling stashes.

    The returned *preview* contains at most *max_lines*/*max_chars* of
    the output followed by a note that references the saved file via
    its workspace-relative path (e.g. ``.egg_outputs/<thread_id>/abc.txt``),
    which resolves identically inside and outside the sandbox.

    Returns ``(preview, "")`` if the file could not be written — the
    preview will still be returned so the caller can proceed.
    """
    if not isinstance(full_output, str):
        full_output = str(full_output or "")

    # Resolve the stash directory under the workspace root, not under a
    # per-thread subdirectory.  Sandboxes/REPLs mask .egg_outputs and expose
    # only the allowed thread/subtree stash directories explicitly; placing the
    # store at the root keeps paths stable and avoids nesting inaccessible
    # .egg_outputs directories inside child working dirs.
    saved_path = ""  # absolute host path (for runner diagnostics)
    relative_path = ""  # workspace-relative path (for the LLM)
    try:
        from pathlib import Path as _Path
        workspace = _Path.cwd().resolve()
        from .output_paths import thread_output_dir

        out_dir = thread_output_dir(db, workspace, thread_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        tid_suffix = str(thread_id or "thread")[-8:]
        tcid_suffix = str(tool_call_id or "tc")[-8:]
        ts = int(time.time())
        rand = os.urandom(4).hex()
        path = out_dir / f"{tid_suffix}_{tcid_suffix}_{ts}_{rand}.txt"
        path.write_text(full_output, encoding="utf-8")
        # Restrict to owner-readable so even in bwrap the file is less
        # casually visible to other users on a multi-user host. Does
        # not protect against a sandboxed process running as the same
        # uid — that's a bwrap-level concern.
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        saved_path = str(path)
        try:
            relative_path = str(path.relative_to(workspace))
        except ValueError:
            # Fallback: file isn't under the workspace — emit the absolute
            # path and hope the reader is on the same filesystem.
            relative_path = saved_path
    except Exception:
        saved_path = ""
        relative_path = ""

    lines = full_output.splitlines()
    line_count = len(lines)
    char_count = len(full_output)

    preview = full_output
    if line_count > max_lines:
        preview = "\n".join(lines[:max_lines])
    if len(preview) > max_chars:
        preview = preview[:max_chars]

    if preview != full_output:
        preview = preview.rstrip()
        if relative_path:
            preview += (
                f"\n\n[Preview only — first {min(line_count, max_lines)} lines / "
                f"{min(char_count, max_chars)} chars of {line_count} lines, "
                f"{char_count} chars total. Full output saved (workspace-relative) "
                f"to: {relative_path}. Read this file from the workspace root "
                "(e.g. with cat/head/tail) if you need the complete content — "
                "the path resolves identically inside or outside any sandbox.]"
            )
        else:
            preview += (
                f"\n\n[Output truncated for preview ({line_count} lines, "
                f"{char_count} chars); full output could not be saved to disk.]"
            )

    return preview, saved_path


def _emit_auto_output_approval(db, thread_id: str, tool_call_id: str, full_output: str) -> None:
    """Emit a ``tool_call.output_approval`` event for a finished tool call.

    No-op if an explicit decision already exists (e.g. user-cancelled
    via Ctrl+C). Small outputs are approved as ``whole``; large outputs
    are stashed to disk via :func:`stash_tool_output_and_build_preview`
    and approved as ``partial`` with a preview+file-path note so the
    LLM can retrieve the full content on demand.
    """
    try:
        from .tool_state import build_tool_call_states
        states_now = build_tool_call_states(db, thread_id)
        existing = states_now.get(str(tool_call_id))
        has_decision = bool(getattr(existing, "output_decision", None))
    except Exception:
        has_decision = False
    if has_decision:
        return

    if not isinstance(full_output, str):
        full_output = str(full_output or "")
    try:
        from .output_policy import OutputPolicyRequest, create_output_policy_registry, decide_output_publication

        publication = decide_output_publication(
            create_output_policy_registry(),
            OutputPolicyRequest(
                db=db,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                output=full_output,
                limits={
                    "long_output_line_threshold": LONG_OUTPUT_LINE_THRESHOLD,
                    "long_output_char_threshold": LONG_OUTPUT_CHAR_THRESHOLD,
                    "preview_max_lines": PREVIEW_MAX_LINES,
                    "preview_max_chars": PREVIEW_MAX_CHARS,
                },
            ),
        )
        preview = publication.preview
        decision = publication.decision
        reason = publication.reason
        channels = dict(publication.channels or {})
        artifact_path = publication.artifact_path
    except Exception:
        line_count = len(full_output.splitlines())
        char_count = len(full_output)
        is_long = (
            line_count > LONG_OUTPUT_LINE_THRESHOLD
            or char_count > LONG_OUTPUT_CHAR_THRESHOLD
        )

        if is_long:
            preview, saved = stash_tool_output_and_build_preview(
                db, thread_id, tool_call_id, full_output,
            )
            decision = "partial"
            reason = (
                f"Auto: output too long ({line_count} lines, {char_count} chars) — "
                f"stashed to {saved}" if saved else
                f"Auto: output too long ({line_count} lines, {char_count} chars); "
                "stash failed, sending preview only"
            )
            artifact_path = saved
        else:
            preview = full_output
            decision = "whole"
            reason = "Auto: output below size thresholds"
            artifact_path = ""
        channels = {}

    try:
        db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=thread_id,
            type_="tool_call.output_approval",
            msg_id=None,
            invoke_id=None,
            payload={
                "tool_call_id": tool_call_id,
                "decision": decision,
                "reason": reason,
                "preview": preview,
                "channels": channels,
                "artifact_path": artifact_path,
            },
        )
    except Exception:
        pass


def emit_tool_stream_delta(
    db,
    *,
    thread_id: str,
    invoke_id: str,
    tool_call_id: str,
    tool_name: str = "",
    text: str = "",
    current_model: Optional[str] = None,
    suppressed: bool = False,
    chunk_seq: Optional[int] = None,
) -> None:
    """Append one tool-output stream.delta event."""
    payload_tool: Dict[str, Any] = {
        'name': tool_name or '',
        'id': tool_call_id,
    }
    if suppressed:
        payload_tool['suppressed'] = True
    else:
        payload_tool['text'] = text
    payload: Dict[str, Any] = {'tool': payload_tool, 'model_key': current_model}
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_='stream.delta',
        invoke_id=invoke_id,
        chunk_seq=chunk_seq if chunk_seq is not None else db.max_chunk_seq(invoke_id) + 1,
        payload=payload,
    )


def tool_timeout_summary(
    tool_name: str,
    timeout_sec: Optional[float],
    started_at: float,
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """Return a one-line timeout countdown for a running tool.

    ``None`` means no timeout is active, so callers should not emit a
    ``tool_call.summary`` event.  Keeping this as a small pure helper avoids
    duplicating countdown formatting in bash and Python-tool execution paths.
    """
    if timeout_sec is None:
        return None
    try:
        limit = float(timeout_sec)
        if limit <= 0:
            return None
        start = float(started_at)
        current = time.time() if now is None else float(now)
    except Exception:
        return None
    elapsed = max(0.0, current - start)
    remaining = max(0.0, limit - elapsed)
    name = str(tool_name or 'tool')
    return f"{name} running; timeout in {remaining:.0f}s (limit {limit:.0f}s)"


def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Return tool arguments as a dict without mutating caller-owned data.

    Tool arguments enter the runner from provider ``tool_calls`` and may be a
    JSON string, an already-decoded dict, or an arbitrary scalar.  Bash and the
    generic tool path both need the same interpretation, so keep the coercion in
    one place instead of letting timeout handling drift between paths.
    """
    if isinstance(arguments, str):
        try:
            return json.loads(arguments) if arguments.strip() else {}
        except Exception:
            return {"script": arguments}
    if isinstance(arguments, dict):
        return dict(arguments)
    return {"script": str(arguments)}


def resolve_tool_timeout_sec(
    arguments: Any,
    config_timeout_sec: Optional[float] = None,
    default_timeout_sec: Optional[float] = None,
) -> Optional[float]:
    """Resolve the effective positive timeout for a tool call.

    Priority is intentionally shared by bash and non-bash execution paths:

    1. LLM/tool-call supplied ``timeout_sec`` when it parses as a positive
       number.
    2. ``RunnerConfig.tool_timeout_sec`` when set to a positive number.
    3. Global default timeout when set to a positive number.

    ``None`` means no active timeout.  Invalid or non-positive LLM values are
    treated as absent and fall back to the next configured source; this matches
    the intent documented in the tool schemas and avoids misleading countdowns.
    """
    args = parse_tool_arguments(arguments)
    candidates = (args.get('timeout_sec'), config_timeout_sec, default_timeout_sec)
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def emit_tool_summary_event(
    db,
    *,
    thread_id: str,
    invoke_id: Optional[str],
    tool_call_id: str,
    tool_name: str = "",
    summary: str,
) -> None:
    """Append a persisted tool_call.summary event for live status display."""
    if not isinstance(summary, str) or not summary:
        return
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_='tool_call.summary',
        invoke_id=invoke_id,
        payload={
            'tool_call_id': tool_call_id,
            'name': tool_name or 'tool',
            'summary': summary,
        },
    )


def emit_limited_tool_stream_delta(
    db,
    limiter: ToolStreamPreviewLimiter,
    text: str,
    *,
    thread_id: str,
    invoke_id: str,
    tool_call_id: str,
    tool_name: str = "",
    current_model: Optional[str] = None,
    heartbeat,
    suppressed_counter: Optional[Dict[str, int]] = None,
    next_chunk_seq: Optional[Callable[[], int]] = None,
) -> bool:
    """Emit bounded live preview for a tool-output chunk.

    Returns False when the caller should stop because the stream lease was
    lost; otherwise True.
    """
    preview_text, just_suppressed = limiter.filter(text)
    if suppressed_counter is not None:
        suppressed_counter.setdefault('count', 0)
    if just_suppressed:
        preview_text += tool_stream_suppressed_notice(tool_name)
        if not heartbeat():
            return False
        emit_tool_stream_delta(
            db,
            thread_id=thread_id,
            invoke_id=invoke_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            current_model=current_model,
            suppressed=True,
            chunk_seq=next_chunk_seq() if next_chunk_seq is not None else None,
        )
        if suppressed_counter is not None:
            suppressed_counter['count'] = int(suppressed_counter.get('count') or 0) + 1
    elif not preview_text and limiter.suppressed and suppressed_counter is not None:
        suppressed_counter['count'] = int(suppressed_counter.get('count') or 0) + 1
        if limiter.should_emit_indicator(suppressed_counter['count']):
            if not heartbeat():
                return False
            emit_tool_stream_delta(
                db,
                thread_id=thread_id,
                invoke_id=invoke_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                current_model=current_model,
                suppressed=True,
                chunk_seq=next_chunk_seq() if next_chunk_seq is not None else None,
            )
    if not preview_text:
        return True
    if not heartbeat():
        return False
    emit_tool_stream_delta(
        db,
        thread_id=thread_id,
        invoke_id=invoke_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        text=preview_text,
        current_model=current_model,
        chunk_seq=next_chunk_seq() if next_chunk_seq is not None else None,
    )
    return True


def _no_api_calls_mode(cfg: Optional['RunnerConfig'] = None) -> bool:
    """Check if NO_API_CALLS mode is enabled (read-only viewing mode).

    When enabled:
    - RA1 (LLM API calls) are blocked
    - RA2 (assistant tool calls) are blocked
    - RA3 (user commands) are still allowed
    """
    if os.environ.get('NO_API_CALLS', '').lower() in ('1', 'true', 'yes'):
        return True
    if cfg and cfg.no_api_calls:
        return True
    return False


# Default tool execution timeout (seconds). 0 or negative means no timeout.
_default_tool_timeout_sec: float = 30.0


def set_default_tool_timeout(timeout_sec: float) -> None:
    """Set the global default timeout for tool execution (bash, python, etc.).

    This timeout is used when:
    - RunnerConfig.tool_timeout_sec is not set
    - LLM does not specify timeout_sec in the tool call

    Args:
        timeout_sec: Timeout in seconds. Use 0 or negative to disable timeout.
    """
    global _default_tool_timeout_sec
    _default_tool_timeout_sec = timeout_sec


def get_default_tool_timeout() -> float:
    """Get the current global default timeout for tool execution.

    Returns:
        Timeout in seconds. 0 or negative means no timeout.
    """
    return _default_tool_timeout_sec


@dataclass
class RunnerConfig:
    lease_ttl_sec: int = 10
    heartbeat_sec: float = 1.0
    max_concurrent_threads: int = 4
    # Explicit RLM scheduling model: only RA1/LLM work consumes scarce
    # LLM slots.  ``max_concurrent_threads`` remains as the backwards-
    # compatible default for this field when it is left unset.
    max_concurrent_llm_threads: Optional[int] = None
    # Optional cap for concurrently running tool turns (RA2/RA3).  None
    # means tool turns do not consume a global scheduler slot; they are
    # still per-thread leased/running/interruptible.
    max_concurrent_tool_threads: Optional[int] = None
    # Sticky scheduling options
    sticky_scheduling: bool = False  # opt-in to slot reservation
    sticky_idle_threshold_sec: float = 5.0  # idle time before losing reserved slot
    # Priority mode: "none" | "alphabetical" (tie-breaker for equal priorities)
    priority_mode: str = "none"
    # API timeout: None = 600s default, 0 = no timeout, >0 = timeout in seconds
    # Per-thread settings (via thread.scheduling events) override this
    api_timeout_sec: Optional[float] = None
    # Tool execution timeout: None = 600s default, 0 = no timeout, >0 = timeout in seconds
    tool_timeout_sec: Optional[float] = None
    # Read-only mode: block RA1/RA2, allow RA3 (overridden by NO_API_CALLS env var)
    no_api_calls: bool = False
    # Global context limit: None = no limit, >0 = max tokens before LLM call is rejected
    # Per-thread settings (via thread.context_limit events) override this
    context_limit: Optional[int] = None
    # First automatic compaction policy: when explicitly set, this overrides
    # model/env/default threshold resolution.  Positive values enable
    # auto-compaction at the RA1 turn boundary; zero/negative values disable
    # it from runner config so lower-precedence sources are not used.
    auto_compact_threshold_tokens: Optional[int] = None

    @property
    def effective_max_concurrent_llm_threads(self) -> int:
        return int(self.max_concurrent_llm_threads or self.max_concurrent_threads)


class ThreadRunner:
    """Runs a single thread by acquiring the per-thread lease (open_streams with invoke_id fence)
    and streaming assistant output.
    """

    def __init__(self, db: ThreadsDB, thread_id: str, llm: Optional[LLMClient] = None, owner: Optional[str] = None, purpose: str = "assistant_stream", config: Optional[RunnerConfig] = None,
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None):
        self.db = db
        self.thread_id = thread_id
        if llm is not None:
            self.llm = llm
        elif LLMClient is not None:
            self.llm = LLMClient(models_path=models_path or 'models.json', all_models_path=all_models_path or 'all-models.json')
        else:
            self.llm = None
        self.owner = owner or os.environ.get("USER") or "runner"
        self.purpose = purpose
        self.cfg = config or RunnerConfig()
        self.tools = tools or create_default_tools()
        self.models_path = models_path or 'models.json'
        self.all_models_path = all_models_path or 'all-models.json'

    def _evaluate_pending_approval_policies(self) -> None:
        """Let deterministic approval policies advise on TC1 tool calls."""

        try:
            from .approval import (
                APPROVAL_ALLOW,
                APPROVAL_DENY,
                create_approval_policy_registry,
                evaluate_approval_policies,
                request_from_tool_call_state,
            )
            from .api import approve_tool_calls_for_thread
            from .tool_state import build_tool_call_states
        except Exception:
            return

        registry = create_approval_policy_registry()
        changed = False
        for tc in build_tool_call_states(self.db, self.thread_id).values():
            if tc.state != "TC1":
                continue
            origin = "user_command" if tc.parent_role == "user" else "assistant"
            request = request_from_tool_call_state(self.db, self.thread_id, tc, origin=origin)
            # Assistant-originated calls still fall back to the existing human
            # approval flow; audit auto-allow decisions that change state.
            verdict = evaluate_approval_policies(registry, request, audit=(origin == "user_command"))
            if verdict.decision == APPROVAL_ALLOW:
                approve_tool_calls_for_thread(
                    self.db,
                    self.thread_id,
                    decision="granted",
                    reason=verdict.reason or f"Approved by policy {verdict.policy}",
                    tool_call_id=tc.tool_call_id,
                )
                changed = True
            elif verdict.decision == APPROVAL_DENY:
                approve_tool_calls_for_thread(
                    self.db,
                    self.thread_id,
                    decision="denied",
                    reason=verdict.reason or f"Denied by policy {verdict.policy}",
                    tool_call_id=tc.tool_call_id,
                )
                changed = True
        if changed:
            try:
                from .tool_state import _REDUCER_CACHE  # type: ignore

                for key in list(_REDUCER_CACHE.keys()):
                    if key[0] == str(self.db.path) and key[1] == self.thread_id:
                        del _REDUCER_CACHE[key]
            except Exception:
                pass

    async def run_once(self) -> bool:
        """Attempt one assistant step (RA1/RA2/RA3) if runnable.

        Uses discover_runner_actionable() to decide what work to perform,
        acquires a lease with a fresh invoke_id, and records stream/open
        and stream/delta/stream/close events. Returns True if any work was
        performed, False if the thread was idle or paused.
        """
        # Respect paused threads
        th = self.db.get_thread(self.thread_id)
        if th and th.status == 'paused':
            return False

        # Let approval policies auto-resolve safe TC1 calls before asking
        # discover_runner_actionable() what work is runnable.
        try:
            self._evaluate_pending_approval_policies()
        except Exception:
            pass

        # Determine what kind of work (if any) is pending.  The cached
        # variant avoids repeatedly rebuilding tool-call state when the
        # event log has not changed for this thread.
        ra = discover_runner_actionable_cached(self.db, self.thread_id)
        if not ra:
            return False

        # Block RA1 and RA2 in NO_API_CALLS mode (read-only viewing mode)
        if _no_api_calls_mode(self.cfg):
            if ra.kind in ('RA1_llm', 'RA2_tools_assistant'):
                return False  # Skip silently - thread appears idle
            # RA3_tools_user is allowed through

        # Acquire lease with fresh invoke_id
        invoke_id = os.urandom(10).hex()
        lease_until = _now_plus(self.cfg.lease_ttl_sec)

        # Important: purpose is per-invoke, not per-process. We use it
        # to distinguish LLM streaming (RA1) from tool execution
        # streaming (RA2/RA3) so Ctrl+C interrupts can advance the RA1
        # boundary even when interrupted before the first delta.
        purpose = 'llm' if ra.kind == 'RA1_llm' else 'tool'

        if not self.db.try_open_stream(self.thread_id, invoke_id, lease_until, owner=self.owner, purpose=purpose):
            return False

        # Resolve current model for this turn from eggthreads API so that
        # the provider call and the event annotations stay in sync. Fall
        # back to the LLM client's current_model_key if needed.
        current_model: Optional[str] = None
        concrete_model_info: Optional[Dict[str, Any]] = None
        try:
            from .api import current_thread_model, current_thread_model_info
            current_model = current_thread_model(self.db, self.thread_id)
            concrete_model_info = current_thread_model_info(self.db, self.thread_id)
        except Exception:
            current_model = None
            concrete_model_info = None
        if not current_model:
            try:
                current_model = getattr(self.llm, 'current_model_key', None)
            except Exception:
                current_model = None

        # For LLM turns, configure the underlying client before we start
        # streaming so that the model used for the provider call matches
        # the model we record in events.
        if ra.kind == 'RA1_llm' and current_model:
            try:
                if concrete_model_info:
                    # Try set_model_with_config if available (eggllm >= 0.1.0)
                    if hasattr(self.llm, 'set_model_with_config'):
                        self.llm.set_model_with_config(current_model, concrete_model_info)
                    else:
                        self.llm.set_model(current_model)
                else:
                    self.llm.set_model(current_model)
            except Exception:
                pass

        # Open streaming event tagged with model_key and kind so that
        # downstream boundary detection can distinguish RA1 from
        # tool streaming.
        self.db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=self.thread_id,
            type_='stream.open',
            msg_id=os.urandom(10).hex(),
            invoke_id=invoke_id,
            payload={'model_key': current_model, 'stream_kind': purpose, 'ra_kind': ra.kind},
        )

        # Heartbeat loop to keep lease alive
        stop_flag = False

        async def hb():
            nonlocal stop_flag
            while not stop_flag:
                await asyncio.sleep(self.cfg.heartbeat_sec)
                if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                    stop_flag = True
                    return

        hb_task = asyncio.create_task(hb())

        # Shared helpers
        chunk_seq = self.db.max_chunk_seq(invoke_id)

        def _append_delta(payload: Dict[str, Any]):
            nonlocal chunk_seq
            chunk_seq += 1
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='stream.delta',
                invoke_id=invoke_id,
                chunk_seq=chunk_seq,
                payload=payload,
            )

        # Get context limit for this thread (with ancestor inheritance), fall back to config
        context_limit: Optional[int] = None
        try:
            from .api import get_context_limit
            context_limit = get_context_limit(self.db, self.thread_id)
        except Exception:
            context_limit = None
        # Fall back to global config if no per-thread limit is set
        if context_limit is None and self.cfg.context_limit is not None:
            context_limit = self.cfg.context_limit

        was_cancelled = False
        context_length_error: Optional[str] = None
        try:
            if ra.kind == 'RA1_llm':
                # Check context limit before making LLM call
                if context_limit:
                    try:
                        from .token_count import thread_token_stats
                        stats = thread_token_stats(self.db, self.thread_id)
                        current_tokens = stats.get('context_tokens', 0)
                        if current_tokens >= context_limit:
                            # Emit error instead of calling API
                            error_msg = f"Context limit exceeded: {current_tokens} tokens >= {context_limit} limit"
                            _append_delta({'reason': error_msg, 'model_key': current_model})
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='msg.create',
                                msg_id=os.urandom(10).hex(),
                                payload={'role': 'system', 'content': f'LLM/runner error: {error_msg}'},
                            )
                            raise ContextLimitExceeded(error_msg)  # Propagate to outer handler
                    except ImportError:
                        pass  # If token_count unavailable, proceed with call (fail-open)
                    except ContextLimitExceeded:
                        raise  # Don't swallow intentional context limit errors
                    except Exception as e:
                        # Log but don't block on token counting errors (fail-open)
                        print(f"Warning: context limit check failed: {e}")

                # ---------------- RA1: LLM call ----------------
                await self._run_ra1_llm(invoke_id, current_model)

            elif ra.kind in ('RA2_tools_assistant', 'RA3_tools_user'):
                # ---------------- RA2/RA3: tool calls ----------------
                # For now we do not stream tool execution output separately
                # via additional LLM calls; we simply execute tools for
                # approved tool calls and advance their states.
                await self._run_ra_tools(invoke_id, current_model, ra)

        except asyncio.CancelledError:
            # Cooperative shutdown/cancellation should not be recorded as an
            # LLM/runner error. Defer re-raising until after stream/lease cleanup.
            was_cancelled = True
        except Exception as e:
            if ra.kind == 'RA1_llm' and _is_context_length_exceeded_error(e):
                context_length_error = str(e) if str(e) else f"{type(e).__name__}: (no message)"
                try:
                    # Advance the failed RA1 boundary; the summary request is
                    # appended after stream.close so it is the next RA1 turn.
                    _append_delta({'reason': f'LLM/runner context length exceeded: {context_length_error}', 'model_key': current_model})
                except Exception:
                    pass
            else:
                # Surface provider/config/network or tool errors into the thread
                # and ensure RA1 boundaries advance even if the provider fails
                # before any streaming deltas are emitted.
                # Ensure we always have a meaningful error message
                error_msg = str(e) if str(e) else f"{type(e).__name__}: (no message)"
                try:
                    # Emit a synthetic stream.delta with a 'reason' field so
                    # _last_stream_close_seq() will treat this invoke_id as an
                    # LLM stream. This prevents the same user message from
                    # repeatedly triggering a failing RA1 turn.
                    _append_delta({'reason': f'LLM/runner error: {error_msg}', 'model_key': current_model})
                except Exception:
                    pass
                try:
                    err_payload = {'role': 'system', 'content': f'LLM/runner error: {error_msg}'}
                    if current_model:
                        err_payload['model_key'] = current_model
                    self.db.append_event(
                        event_id=os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='msg.create',
                        msg_id=os.urandom(10).hex(),
                        payload=err_payload,
                    )
                    print(f"Runner error: {error_msg}")
                except Exception:
                    pass
        finally:
            stop_flag = True
            try:
                hb_task.cancel()
                await asyncio.gather(hb_task, return_exceptions=True)
            except Exception:
                pass

        # Close stream if we still own the lease
        try:
            row = self.db.current_open(self.thread_id)
            still_owner = bool(
                row
                and row['invoke_id'] == invoke_id
                and row['lease_until'] > _utcnow_iso()
            )
        except Exception:
            still_owner = False
        if still_owner:
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='stream.close',
                invoke_id=invoke_id,
                payload={},
            )

        # Rebuild snapshot and short_recap for readability
        try:
            cur = self.db.conn.execute(
                'SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC',
                (self.thread_id,),
            )
            evs = cur.fetchall()
            from .snapshot import SnapshotBuilder

            snap = SnapshotBuilder().build(evs)
            last_seq = evs[-1]['event_seq'] if evs else -1
            self.db.conn.execute(
                'UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?',
                (json.dumps(snap), last_seq, self.thread_id),
            )
            # Extract <short_recap>...</short_recap> from last assistant message
            try:
                def _extract_short(text: str) -> Optional[str]:
                    if not isinstance(text, str):
                        return None
                    end = text.rfind('</short_recap>')
                    if end == -1:
                        return None
                    start = text.rfind('<short_recap>', 0, end)
                    if start == -1:
                        return None
                    inner_start = start + len('<short_recap>')
                    if end < inner_start:
                        return None
                    return text[inner_start:end].strip()

                msgs = snap.get('messages', []) if isinstance(snap, dict) else []
                last_assist = None
                for m in reversed(msgs):
                    if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                        last_assist = m.get('content')
                        break
                rec = _extract_short(last_assist or '') if last_assist else None
                if rec:
                    self.db.conn.execute(
                        'UPDATE threads SET short_recap=? WHERE thread_id=?',
                        (rec, self.thread_id),
                    )
            except Exception:
                pass
        except Exception:
            pass

        if context_length_error is not None:
            try:
                from .api import append_auto_compaction_summary_request, create_snapshot

                create_snapshot(self.db, self.thread_id)
                recovery_selector = str(ra.msg_id or "") or "last_message"
                summary_result = append_auto_compaction_summary_request(
                    self.db,
                    self.thread_id,
                    selector=recovery_selector,
                )
                if not summary_result.success and summary_result.compaction is not None:
                    summary_result = append_auto_compaction_summary_request(
                        self.db,
                        self.thread_id,
                        selector="last_llm",
                    )
                if summary_result.success:
                    create_snapshot(self.db, self.thread_id)
                    print(f"Runner recovered from context-length error: {summary_result.message}")
                else:
                    raise RuntimeError(summary_result.message)
            except Exception as recovery_error:
                try:
                    err_payload = {'role': 'system', 'content': f'LLM/runner error: {recovery_error}'}
                    if current_model:
                        err_payload['model_key'] = current_model
                    self.db.append_event(
                        event_id=os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='msg.create',
                        msg_id=os.urandom(10).hex(),
                        payload=err_payload,
                    )
                    try:
                        from .api import create_snapshot as _create_snapshot

                        _create_snapshot(self.db, self.thread_id)
                    except Exception:
                        pass
                    print(f"Runner error: {recovery_error}")
                except Exception:
                    pass

        has_pending_assistant_tool = False
        if ra.kind == 'RA1_llm' and not was_cancelled:
            try:
                has_pending_assistant_tool = any(
                    tc.parent_role == 'assistant' and not tc.published
                    for tc in build_tool_call_states(self.db, self.thread_id).values()
                )
            except Exception:
                has_pending_assistant_tool = False

        if ra.kind == 'RA1_llm' and not was_cancelled and context_length_error is None and not has_pending_assistant_tool:
            try:
                from .api import auto_compact_summary_enabled, create_snapshot, maybe_auto_compact_thread, resolve_auto_compact_threshold
                from .token_count import provider_context_token_stats

                threshold = resolve_auto_compact_threshold(
                    self.db,
                    self.thread_id,
                    self.cfg.auto_compact_threshold_tokens,
                    models_path=self.models_path,
                    all_models_path=self.all_models_path,
                )
                if threshold.enabled and threshold.threshold_tokens is not None:
                    create_snapshot(self.db, self.thread_id)
                    stats = provider_context_token_stats(self.db, self.thread_id)
                    current_tokens = int(stats.get('context_tokens') or 0)
                    auto_result = maybe_auto_compact_thread(
                        self.db,
                        self.thread_id,
                        threshold_tokens=threshold.threshold_tokens,
                        context_tokens=current_tokens,
                        summary_mode=auto_compact_summary_enabled(),
                    )
                    if auto_result.triggered:
                        create_snapshot(self.db, self.thread_id)
            except Exception as e:
                # Token accounting/auto-compaction is best-effort; do not block
                # the existing runner path on advisory compaction.
                print(f"Warning: auto compaction check failed: {e}")

        # Attempt lease release (no-op if preempted)
        try:
            self.db.release(self.thread_id, invoke_id)
        except Exception:
            pass

        if was_cancelled:
            raise asyncio.CancelledError
        return True

    async def _run_ra1_llm(self, invoke_id: str, current_model: Optional[str]) -> None:
        """Handle RA1: perform a single LLM call, streaming deltas,
        and append the final assistant message with optional tool_calls.
        """
        from .tool_state import _last_stream_close_seq, _iter_messages_after

        # Re-discover the triggering message (first RA1-eligible message)
        last_close = _last_stream_close_seq(self.db, self.thread_id)
        trigger = None
        for ev in _iter_messages_after(self.db, self.thread_id, last_close):
            try:
                payload = json.loads(ev['payload_json']) if isinstance(ev['payload_json'], str) else (ev['payload_json'] or {})
            except Exception:
                payload = {}
            role = payload.get('role')
            keep_user_turn = bool(payload.get('keep_user_turn'))
            no_api = bool(payload.get('no_api'))
            tool_calls = payload.get('tool_calls') or []
            if role == 'tool' and keep_user_turn and no_api:
                try:
                    tcid = payload.get('tool_call_id')
                    state = build_tool_call_states(self.db, self.thread_id).get(tcid)
                except Exception:
                    state = None
                if state is not None and state.parent_role == 'assistant' and state.name == ANSWER_USER_PRESERVE_TURN_TOOL_NAME:
                    trigger = (ev, payload)
                    break
            if role == 'user' and not tool_calls and not keep_user_turn:
                trigger = (ev, payload)
                break
            if role == 'tool' and not keep_user_turn and not no_api:
                trigger = (ev, payload)
                break
        if not trigger:
            return

        ev, payload = trigger
        role = payload.get('role')
        user_content = payload.get('content', '')

        # Build base_messages from snapshot, respecting no_api and
        # applying per-model thinking-content policy/options.
        th = self.db.get_thread(self.thread_id)
        base_messages: List[Dict[str, Any]] = []
        thinking_policy: Optional[str] = None
        thinking_key: Optional[str] = None
        # Some providers (e.g. Gemini 3) require that we round-trip
        # encrypted thought/signature blobs exactly as received.  These
        # blobs are carried under a provider-defined key configured via
        # thinking_content_key.
        # 'send all' | 'last assistant turn'
        encrypted_thinking_mode: Optional[str] = None
        # Resolve per-model options from the registry if possible.
        try:
            opts = self._model_thinking_options(current_model)
            tp = opts.get('thinking_content_policy')
            if isinstance(tp, str) and tp.strip():
                thinking_policy = tp.strip().lower()
            tk = opts.get('thinking_content_key')
            if isinstance(tk, str) and tk.strip():
                thinking_key = tk.strip()
        except Exception:
            thinking_policy = None
            thinking_key = None

        if th and th.snapshot_json:
            try:
                snap = json.loads(th.snapshot_json)
                msgs = snap.get('messages', []) or []
                try:
                    from .api import filter_messages_for_compaction_provider_context

                    msgs = filter_messages_for_compaction_provider_context(
                        self.db,
                        self.thread_id,
                        msgs,
                    )
                except Exception:
                    pass

                # Recognize encrypted-Gemini thinking policies.
                if thinking_policy in ('send all encrypted gemini', 'send_all_encrypted_gemini'):
                    encrypted_thinking_mode = 'send all'
                elif thinking_policy in ('last assistant turn encrypted gemini', 'last_assistant_turn_encrypted_gemini'):
                    encrypted_thinking_mode = 'last assistant turn'

                # If the model wants only the last assistant turn's
                # thinking, identify the index of the last user message
                # so we can treat messages after that as the "tail".
                last_user_idx = -1
                if thinking_policy == 'last assistant turn' or encrypted_thinking_mode == 'last assistant turn':
                    for i, m in enumerate(msgs):
                        try:
                            if m.get('role') == 'user' and isinstance(m.get('content'), str):
                                last_user_idx = i
                        except Exception:
                            continue

                def _maybe_include_reasoning(m: Dict[str, Any], idx: int) -> Optional[str]:
                    """Return reasoning text to send for this message, or None.

                    The snapshot uses ``reasoning`` to store thinking.
                    The provider may expect it under a different key,
                    configured via thinking_content_key.  We return the
                    thinking string here and let the caller attach it
                    under the appropriate key on the outbound message.
                    """
                    raw = m.get('reasoning') or m.get('reasoning_content')
                    if not isinstance(raw, str) or not raw:
                        return None
                    # Encrypted-Gemini modes do not send plaintext reasoning
                    # derived from the provider stream; instead they round-trip
                    # a provider-supplied opaque field under
                    # thinking_content_key.
                    if encrypted_thinking_mode is not None:
                        return None
                    if thinking_policy == 'send all':
                        return raw
                    if thinking_policy == 'last assistant turn':
                        # Only include thinking for messages in the
                        # "tail" after the last user content.
                        if last_user_idx == -1 or idx <= last_user_idx:
                            return None
                        return raw
                    # Default / "strip all": never send.
                    return None

                def _maybe_include_encrypted_thinking(m: Dict[str, Any], idx: int) -> Optional[Any]:
                    """Return opaque provider thinking/signature content to round-trip.

                    The returned value is attached under the configured
                    thinking_content_key without any interpretation.
                    """
                    if encrypted_thinking_mode is None:
                        return None
                    out_thinking_key = thinking_key or 'reasoning_content'
                    if out_thinking_key not in m:
                        return None
                    val = m.get(out_thinking_key)
                    if val is None:
                        return None
                    if encrypted_thinking_mode == 'send all':
                        return val
                    if encrypted_thinking_mode == 'last assistant turn':
                        if last_user_idx == -1 or idx <= last_user_idx:
                            return None
                        return val
                    return None

                def _should_include_reasoning_field(idx: int) -> bool:
                    """Return True if this message index should have reasoning field (even if empty).

                    This applies when a plaintext thinking policy is active that would send
                    reasoning for this message. Providers like DeepSeek require the field to
                    be present even when empty.

                    NOTE: This does NOT apply to encrypted thinking modes (e.g., Gemini),
                    which use structured objects, not strings. Adding an empty string ""
                    would cause "Value is not a struct" errors from those providers.
                    """
                    # Only for plaintext reasoning mode (e.g., DeepSeek)
                    # Encrypted modes (Gemini) use structured objects and don't need this fallback
                    if encrypted_thinking_mode is not None:
                        return False
                    if thinking_policy == 'send all':
                        return True
                    if thinking_policy == 'last assistant turn':
                        return last_user_idx != -1 and idx > last_user_idx
                    return False

                def _passthrough_provider_fields(src: Dict[str, Any], dst: Dict[str, Any]) -> None:
                    """Copy provider-specific fields from a snapshot message.

                    For "encrypted gemini" modes we must be able to
                    round-trip provider-returned blobs (e.g.
                    thought_signature / extra_content) exactly as
                    received.

                    We copy only keys that are *not* eggthreads
                    bookkeeping fields.
                    """
                    if encrypted_thinking_mode is None:
                        return
                    if not isinstance(src, dict) or not isinstance(dst, dict):
                        return
                    ignore = {
                        # OpenAI message protocol keys we always set explicitly
                        'role', 'content', 'tool_calls',
                        # eggthreads snapshot/DB metadata
                        'msg_id', 'ts',
                        # eggthreads-only flags
                        'no_api', 'keep_user_turn',
                        # eggthreads local annotations
                        'model_key', 'reasoning',
                    }
                    for k, v in src.items():
                        if k in ignore:
                            continue
                        if k in dst:
                            continue
                        if v is None:
                            continue
                        dst[k] = v

                for idx, m in enumerate(msgs):
                    if m.get('no_api'):
                        continue
                    if m.get('answer_user_preserve_turn'):
                        continue
                    r = m.get('role')
                    content = m.get('content', '')
                    # Compute optional thinking text according to policy
                    thinking_text = _maybe_include_reasoning(m, idx)
                    encrypted_thinking_val = _maybe_include_encrypted_thinking(m, idx)
                    # Determine the outbound thinking key, defaulting
                    # to the provider's native "reasoning_content" if
                    # no explicit key was configured.
                    out_thinking_key = thinking_key or 'reasoning_content'

                    provider_tcs = _provider_tool_calls(m.get('tool_calls'))

                    if r == 'assistant' and provider_tcs:
                        # Assistant messages with tool_calls may also
                        # carry thinking. We forward tool_calls plus any
                        # allowed thinking under the configured key.
                        # NOTE: content field is required by some providers
                        # (e.g., StepFun) even when empty.
                        msg_out: Dict[str, Any] = {
                            'role': 'assistant',
                            'content': content,
                            'tool_calls': provider_tcs,
                        }
                        if thinking_text is not None:
                            msg_out[out_thinking_key] = thinking_text
                        elif encrypted_thinking_val is not None:
                            msg_out[out_thinking_key] = encrypted_thinking_val
                        elif _should_include_reasoning_field(idx):
                            # Provider requires reasoning field even when empty
                            msg_out[out_thinking_key] = ""
                        _passthrough_provider_fields(m, msg_out)
                        base_messages.append(msg_out)
                    elif r == 'tool':
                        obj = {'role': 'tool', 'content': content}
                        if m.get('name'):
                            obj['name'] = m.get('name')
                        if m.get('tool_call_id'):
                            obj['tool_call_id'] = m.get('tool_call_id')
                        # Preserve user_tool_call so that RA3 user commands
                        # can be rewritten to user-role messages before
                        # hitting the provider API.
                        if m.get('user_tool_call'):
                            obj['user_tool_call'] = m.get('user_tool_call')
                        base_messages.append(obj)
                    elif r in ('system', 'user', 'assistant'):
                        msg_out: Dict[str, Any] = {'role': r, 'content': content}
                        if r == 'assistant' and thinking_text is not None:
                            msg_out[out_thinking_key] = thinking_text
                        elif r == 'assistant' and encrypted_thinking_val is not None:
                            msg_out[out_thinking_key] = encrypted_thinking_val
                        elif r == 'assistant' and _should_include_reasoning_field(idx):
                            # Provider requires reasoning field even when empty
                            msg_out[out_thinking_key] = ""
                        if r == 'assistant':
                            _passthrough_provider_fields(m, msg_out)
                        base_messages.append(msg_out)
            except Exception:
                pass

        # Avoid duplicating trigger if already in snapshot
        try:
            last_seq = int(ev['event_seq'])
            snap_has_last = bool(th and isinstance(th.snapshot_last_event_seq, int) and th.snapshot_last_event_seq >= last_seq)
        except Exception:
            snap_has_last = False
        if not snap_has_last:
            if not payload.get('no_api'):
                if role == 'tool':
                    obj = {'role': 'tool', 'content': user_content}
                    if payload.get('name'):
                        obj['name'] = payload.get('name')
                    if payload.get('tool_call_id'):
                        obj['tool_call_id'] = payload.get('tool_call_id')
                    if payload.get('user_tool_call'):
                        obj['user_tool_call'] = payload.get('user_tool_call')
                    base_messages.append(obj)
                else:
                    base_messages.append({'role': 'user', 'content': user_content})


        assistant_text_parts: List[str] = []
        reasoning_parts: List[str] = []

        recorder = None
        try:
            if os.environ.get('EGGTHREADS_RECORD_PROVIDER'):
                traces_dir = Path('.egg/traces')
                traces_dir.mkdir(parents=True, exist_ok=True)
                ts = _utcnow().strftime('%Y%m%dT%H%M%S')
                rec_path = traces_dir / f"trace_{self.thread_id}_{ts}.jsonl"
                recorder = open(rec_path, 'a', encoding='utf-8')
        except Exception:
            recorder = None

        saw_content_delta = False
        saw_reason_delta = False
        chunk_seq = self.db.max_chunk_seq(invoke_id)

        # Track tool_call arguments as they stream so we can emit
        # incremental deltas into events for live UI rendering.  The
        # OpenAI-compatible adapter accumulates full ``arguments``
        # strings per tool_call; we compute the incremental tail per
        # invoke_id/tool_call_id and store only that tail in
        # stream.delta payloads.
        tool_calls_args_so_far: Dict[str, str] = {}
        tool_calls_names_so_far: Dict[str, str] = {}

        # Final sanitation step before calling the provider: make sure that
        # user messages never carry "tool_calls" fields and that tool
        # exposure honours any per-thread tools configuration (e.g.
        # thread-wide tool disable, per-tool blacklists).
        base_messages = self._sanitize_messages_for_api(base_messages, model_key=current_model)

        # Apply per-thread tools configuration: this governs which tools
        # the LLM is allowed to see in this thread. User-initiated tools
        # (RA3) are still modelled as tool calls but are handled elsewhere
        # when executed.
        tools_cfg = get_thread_tools_config(self.db, self.thread_id)
        tools_spec = self.tools.tools_spec() or None
        if tools_spec is not None:
            # Filter out disabled tool names from the spec before
            # exposing them to the LLM.
            enabled_specs = []
            for spec in tools_spec:
                try:
                    fn = (spec or {}).get('function') or {}
                    name = str(fn.get('name') or '')
                    if name and not tools_cfg.is_tool_allowed(name):
                        continue
                    enabled_specs.append(spec)
                except Exception:
                    enabled_specs.append(spec)
            tools_spec = enabled_specs or None

        # If thread-wide tools are disabled, suppress tools entirely for
        # this RA1 turn.
        if not tools_cfg.llm_tools_enabled:
            tools_spec_to_use = None
            tool_choice = None
        else:
            tools_spec_to_use = tools_spec
            tool_choice = 'auto'

        # Determine API timeout: per-thread setting > config setting > default (600s)
        # 0 or negative means no timeout
        from .api import get_thread_scheduling
        sched_settings = get_thread_scheduling(self.db, self.thread_id)
        if sched_settings.api_timeout is not None:
            api_timeout = sched_settings.api_timeout
        elif self.cfg is not None and self.cfg.api_timeout_sec is not None:
            api_timeout = self.cfg.api_timeout_sec
        else:
            api_timeout = 600  # Default 10 minutes
        # Convert to int for aiohttp; 0 means no timeout
        api_timeout_int = int(api_timeout) if api_timeout > 0 else 0

        interrupted = False
        transport_error_after_output: Optional[BaseException] = None

        def _persist_assistant_message(final: Dict[str, Any]) -> bool:
            """Persist a completed assistant turn and return whether it did.

            Eggllm's documented streaming contract ends with a ``done`` event,
            but a few tests/mocks and some compatibility adapters emit the
            final assistant as a ``message`` event.  Keeping both event shapes
            behind one helper prevents completion detection from drifting: any
            accepted final event creates the same ``msg.create`` boundary that
            ``wait`` observes.
            """

            nonlocal assistant_text_parts, reasoning_parts
            if not isinstance(final, dict):
                final = {}
            if not saw_content_delta:
                fc = final.get('content')
                if isinstance(fc, str) and fc:
                    assistant_text_parts = [fc]
            if not saw_reason_delta:
                fr = final.get('reasoning') or final.get('reason') or final.get('reasoning_content')
                if isinstance(fr, str) and fr:
                    reasoning_parts = [fr]
            assistant_msg: Dict[str, Any] = {'role': 'assistant'}
            if assistant_text_parts:
                assistant_msg['content'] = ''.join(assistant_text_parts)
            tcs = final.get('tool_calls') or []
            if isinstance(tcs, list) and tcs:
                assistant_msg['tool_calls'] = tcs
            if reasoning_parts:
                assistant_msg['reasoning'] = ''.join(reasoning_parts)
            if current_model:
                assistant_msg['model_key'] = current_model

            passthrough_skip_keys = {'role'}
            # ``reasoning_content`` is the provider-facing key for durable
            # reasoning.  Eggthreads normalizes durable reasoning into the
            # local ``reasoning`` field above, so do not persist a duplicate
            # provider key unless a model-specific encrypted/thinking policy
            # explicitly needs raw provider fields to round-trip.
            if not self._should_preserve_provider_reasoning_content(current_model):
                passthrough_skip_keys.add('reasoning_content')

            # Preserve any provider-specific fields returned by eggllm (e.g.
            # Gemini thought signatures). We do not interpret these fields
            # here; we simply persist them so that the next provider request
            # can round-trip them when required by the model/protocol.
            for k, v in final.items():
                if k in passthrough_skip_keys:
                    continue
                if k in assistant_msg:
                    continue
                if v is None:
                    continue
                assistant_msg[k] = v

            # If the provider returned an entirely empty assistant message (no
            # content, no tools, no reasoning), skip creating a blank assistant
            # msg and surface a system notice instead.
            if (not assistant_msg.get('content')
                and not assistant_msg.get('tool_calls')
                and not reasoning_parts
                and not assistant_msg.get('reasoning')
                and not assistant_msg.get('reasoning_content')):
                err_payload: Dict[str, Any] = {
                    'role': 'system',
                    'content': 'LLM error: empty assistant message returned by provider',
                }
                if current_model:
                    err_payload['model_key'] = current_model
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=err_payload,
                )
                return False

            try:
                from .token_count import llm_message_tps_for_invoke
                tps = llm_message_tps_for_invoke(
                    self.db,
                    invoke_id,
                    content=str(assistant_msg.get('content') or ''),
                    reasoning=str(assistant_msg.get('reasoning') or ''),
                    tool_calls=assistant_msg.get('tool_calls') if isinstance(assistant_msg.get('tool_calls'), list) else None,
                )
                if isinstance(tps, float) and tps > 0:
                    assistant_msg['tps'] = tps
            except Exception:
                pass
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='msg.create',
                msg_id=os.urandom(10).hex(),
                payload=assistant_msg,
            )
            return True

        try:
            async for raw in self.llm.astream_chat(base_messages, tools=tools_spec_to_use, tool_choice=tool_choice, timeout=api_timeout_int):
                try:
                    if recorder is not None:
                        recorder.write(json.dumps(raw, ensure_ascii=False) + "\n")
                        recorder.flush()
                except Exception:
                    pass
                if isinstance(raw, list):
                    evts = [e for e in raw if isinstance(e, dict)]
                elif isinstance(raw, dict):
                    evts = [raw]
                else:
                    continue
                for evt in evts:
                    et = evt.get('type')
                    if et == 'content_delta':
                        saw_content_delta = True
                        content = evt.get('text', '')
                        if content:
                            assistant_text_parts.append(content)
                            # Heartbeat / lease extension; stop if we lost lease
                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                interrupted = True
                                break
                            chunk_seq += 1
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='stream.delta',
                                invoke_id=invoke_id,
                                chunk_seq=chunk_seq,
                                payload={'text': content, 'model_key': current_model},
                            )
                            await asyncio.sleep(0)
                    elif et in ('reasoning_delta', 'reasoning_summary_delta'):
                        is_reasoning_summary = et == 'reasoning_summary_delta'
                        if not is_reasoning_summary:
                            saw_reason_delta = True
                        reason = evt.get('text', '')
                        if reason:
                            if not is_reasoning_summary:
                                reasoning_parts.append(reason)
                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                interrupted = True
                                break
                            chunk_seq += 1
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='stream.delta',
                                invoke_id=invoke_id,
                                chunk_seq=chunk_seq,
                                payload={'reasoning_summary': reason, 'model_key': current_model}
                                if is_reasoning_summary else {'reason': reason, 'model_key': current_model},
                            )
                            await asyncio.sleep(0)
                    elif et == 'tool_calls_delta':
                        # Stream tool_call arguments so that the live
                        # chat panel can display them as they arrive.
                        tcs = evt.get('delta') or []
                        if not isinstance(tcs, list):
                            tcs = []
                        for tc_delta in tcs:
                            if not isinstance(tc_delta, dict):
                                continue
                            tcid = str(tc_delta.get('id') or '')
                            fn = tc_delta.get('function') or {}
                            name = fn.get('name') or ''
                            if name:
                                tool_calls_names_so_far[tcid] = str(name)
                            args_full = fn.get('arguments') or ''
                            if not isinstance(args_full, str):
                                try:
                                    args_full = json.dumps(args_full, ensure_ascii=False)
                                except Exception:
                                    args_full = str(args_full)
                            prev = tool_calls_args_so_far.get(tcid, '')
                            if len(args_full) <= len(prev):
                                continue
                            delta_text = args_full[len(prev):]
                            if not delta_text:
                                continue
                            tool_calls_args_so_far[tcid] = args_full
                            # Heartbeat and stop if we lose the lease.
                            if not self.db.heartbeat(self.thread_id, invoke_id, _now_plus(self.cfg.lease_ttl_sec)):
                                interrupted = True
                                break
                            chunk_seq += 1
                            self.db.append_event(
                                event_id=os.urandom(10).hex(),
                                thread_id=self.thread_id,
                                type_='stream.delta',
                                invoke_id=invoke_id,
                                chunk_seq=chunk_seq,
                                payload={
                                    'tool_call': {
                                        'id': tcid,
                                        'name': name,
                                        'arguments_delta': delta_text,
                                    },
                                    'model_key': current_model,
                                },
                            )
                            await asyncio.sleep(0)
                        if interrupted:
                            break
                    elif et in ('done', 'message'):
                        final = evt.get('message') if et == 'done' else evt
                        if not isinstance(final, dict):
                            final = {}
                        _persist_assistant_message(final)
                        return
                if interrupted:
                    break
        except Exception as e:
            if assistant_text_parts or reasoning_parts or tool_calls_args_so_far:
                transport_error_after_output = e
            else:
                raise
        finally:
            # If the stream was interrupted (e.g. via Ctrl+C removing the
            # lease), we still want to persist whatever partial assistant
            # content we have as a user-visible message so that users can
            # inspect or edit it and the model can see what was interrupted.
            if interrupted and (assistant_text_parts or reasoning_parts):
                assistant_msg: Dict[str, Any] = {'role': 'assistant'}
                if assistant_text_parts:
                    assistant_msg['content'] = ''.join(assistant_text_parts)
                if reasoning_parts:
                    assistant_msg['reasoning'] = ''.join(reasoning_parts)
                if current_model:
                    assistant_msg['model_key'] = current_model
                try:
                    from .token_count import llm_message_tps_for_invoke
                    tps = llm_message_tps_for_invoke(
                        self.db,
                        invoke_id,
                        content=str(assistant_msg.get('content') or ''),
                        reasoning=str(assistant_msg.get('reasoning') or ''),
                    )
                    if isinstance(tps, float) and tps > 0:
                        assistant_msg['tps'] = tps
                except Exception:
                    pass
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=assistant_msg,
                )
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    pass

        if transport_error_after_output is not None:
            partial_msg: Dict[str, Any] = {'role': 'assistant'}
            if assistant_text_parts:
                partial_msg['content'] = ''.join(assistant_text_parts)
            if reasoning_parts:
                partial_msg['reasoning'] = ''.join(reasoning_parts)
            if current_model:
                partial_msg['model_key'] = current_model
            if tool_calls_args_so_far:
                partial_tool_calls = []
                for tcid, args_full in tool_calls_args_so_far.items():
                    partial_tool_calls.append({
                        'id': tcid,
                        'type': 'function',
                        'function': {
                            'name': tool_calls_names_so_far.get(tcid, ''),
                            'arguments': args_full,
                        },
                    })
                partial_msg['tool_calls'] = partial_tool_calls
            partial_msg['incomplete'] = True
            partial_msg['incomplete_reason'] = f'provider stream ended early: {transport_error_after_output}'
            self.db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=self.thread_id,
                type_='msg.create',
                msg_id=os.urandom(10).hex(),
                payload=partial_msg,
            )


    def _get_tool_call_id_normalization_strategy(self, model_key: Optional[str]) -> Optional[str]:
        """Get tool_call_id normalization strategy from provider/model config.

        Looks for ``normalize_tool_call_ids`` field first in model config,
        then in provider config. Returns the strategy name (e.g., "mistral9")
        or None if not configured.
        """
        if not model_key or self.llm is None:
            return None
        try:
            from eggllm import LLMClient as _LLMClient  # type: ignore
            if not isinstance(self.llm, _LLMClient):
                return None
            # Check model-level config first
            mc = self.llm.registry.get_model_config(model_key)
            if mc.get('normalize_tool_call_ids'):
                return str(mc['normalize_tool_call_ids'])
            # Fall back to provider-level config
            provider = mc.get('provider')
            if provider:
                pc = self.llm.registry.provider_config(provider)
                if pc.get('normalize_tool_call_ids'):
                    return str(pc['normalize_tool_call_ids'])
        except Exception:
            pass
        return None

    def _model_thinking_options(self, model_key: Optional[str]) -> Dict[str, Any]:
        """Return eggllm model options used for thinking/reasoning policy."""
        if not model_key or self.llm is None:
            return {}
        try:
            from eggllm import LLMClient as _LLMClient  # type: ignore
            if not isinstance(self.llm, _LLMClient):
                return {}
            opts = self.llm.registry.model_options(model_key)  # type: ignore[attr-defined]
            return opts if isinstance(opts, dict) else {}
        except Exception:
            return {}

    def _should_preserve_provider_reasoning_content(self, model_key: Optional[str]) -> bool:
        """Whether final ``reasoning_content`` should be kept as provider data.

        Plaintext reasoning is stored locally as ``reasoning`` and then sent
        under the model's configured thinking key when policy allows. Keeping a
        duplicate ``reasoning_content`` field makes display-only summary safety
        depend on every provider adapter never populating that key by mistake.

        The exception is encrypted/provider-opaque thinking modes where the
        configured key may itself be ``reasoning_content`` and must round-trip
        exactly.
        """
        opts = self._model_thinking_options(model_key)
        policy = opts.get('thinking_content_policy')
        key = opts.get('thinking_content_key')
        if not isinstance(policy, str) or not isinstance(key, str):
            return False
        policy_norm = policy.strip().lower()
        key_norm = key.strip()
        return bool(
            key_norm == 'reasoning_content'
            and policy_norm in (
                'send all encrypted gemini',
                'send_all_encrypted_gemini',
                'last assistant turn encrypted gemini',
                'last_assistant_turn_encrypted_gemini',
            )
        )


    def _sanitize_messages_for_api(self, messages: List[Dict[str, Any]], model_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a sanitized copy of messages for provider API.

        Responsibilities that belong specifically to eggthreads (and not
        to eggllm which is reused by other programs):

        - Convert RA3 user-command tool outputs (role="tool",
          user_tool_call=True) into plain user messages. The provider
          should never see these as tool-role messages; instead, they
          should look like "the user ran this command and saw this text".

        - Strip any ``tool_calls`` field from *user* messages so that
          user commands appear as ordinary user turns in the provider
          protocol.

        We intentionally **do not** touch assistant messages here,
        since their ``tool_calls`` and tool-role responses are the
        standard OpenAI-compatible tools protocol (RA2).
        """
        # When reconstructing provider API messages, we must ensure tool
        # outputs do not leak secrets to the provider. We also sanitize
        # control characters to keep providers and downstream tooling
        # robust.
        try:
            tools_cfg = get_thread_tools_config(self.db, self.thread_id)
            allow_raw = bool(getattr(tools_cfg, 'allow_raw_tool_output', False))
        except Exception:
            allow_raw = False

        # Get tool_call_id normalization strategy for this provider (e.g., "mistral9")
        normalize_strategy = self._get_tool_call_id_normalization_strategy(model_key)

        out: List[Dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            m2 = dict(m)
            if m2.get("answer_user_preserve_turn"):
                continue
            role = m2.get("role")

            if role == "assistant" and isinstance(m2.get("tool_calls"), list):
                filtered_tool_calls = _provider_tool_calls(m2.get("tool_calls"))
                if filtered_tool_calls:
                    m2["tool_calls"] = filtered_tool_calls
                else:
                    m2.pop("tool_calls", None)

            # RA3: user-command tool outputs -> plain user messages
            if role == "tool" and m2.get("user_tool_call") and not m2.get("no_api"):
                content = m2.get("content", "")
                # Mask secrets for provider API unless explicitly allowed.
                if isinstance(content, str) and not allow_raw:
                    try:
                        content = self._filter_tool_output(content, mask_secrets=True)
                    except Exception:
                        pass
                elif isinstance(content, str):
                    # Even in raw mode, still sanitize control chars.
                    try:
                        content = self._filter_tool_output(content, mask_secrets=False)
                    except Exception:
                        pass
                m2 = {"role": "user", "content": content}
                role = "user"

            # User messages must not carry tool_calls when sent to provider
            if role == "user" and "tool_calls" in m2:
                m2.pop("tool_calls", None)

            # For real tool outputs (role="tool" in the tools protocol),
            # mask secrets before sending to the provider unless raw mode
            # is explicitly enabled. This protects against accidental
            # leakage of credentials produced by tools.
            if role == "tool" and not m2.get("no_api"):
                content = m2.get("content")
                if isinstance(content, str):
                    if allow_raw:
                        try:
                            m2["content"] = self._filter_tool_output(content, mask_secrets=False)
                        except Exception:
                            pass
                    else:
                        try:
                            m2["content"] = self._filter_tool_output(content, mask_secrets=True)
                        except Exception:
                            pass

            # Some providers are strict about assistant/tool pairing and
            # will error if they see assistant messages with no content
            # between an assistant(tool_calls) and a tool message.  Blank
            # assistant messages carry no information, so we drop them
            # here to avoid confusing such templates.
            #
            # However, some providers (notably Gemini 3) may return an
            # *empty-content* assistant message that still carries
            # provider-specific fields (e.g. thought signatures) that must
            # be preserved verbatim for the next request. In that case we
            # must keep the message even if content is blank.
            if role == "assistant":
                text = m2.get("content")
                is_blank = (text is None or (isinstance(text, str) and not text.strip()))
                if is_blank and not m2.get("tool_calls"):
                    # Keep if any non-trivial fields remain (e.g.
                    # reasoning_content, thought_signature, extra_content).
                    ignore = {
                        "role",
                        "content",
                        # Removed by eggllm's provider-layer sanitization
                        "model_key",
                        "local_tool",
                        # Eggthreads-local control flags
                        "no_api",
                        "keep_user_turn",
                        "answer_user_preserve_turn",
                    }
                    extra_keys = [k for k in m2.keys() if k not in ignore]
                    if not extra_keys:
                        continue

            # Normalize tool_call_id values if provider requires specific format.
            # IMPORTANT: We must deep-copy tool_calls to avoid mutating the original
            # message dicts, which could affect state tracking in build_tool_call_states.
            if normalize_strategy:
                # Normalize tool_call_id in tool messages
                if role == "tool" and m2.get("tool_call_id"):
                    m2["tool_call_id"] = normalize_tool_call_id(m2["tool_call_id"], normalize_strategy)
                # Normalize tool_calls[].id in assistant messages (deep copy to avoid mutation)
                if role == "assistant" and m2.get("tool_calls"):
                    normalized_tcs = []
                    for tc in m2["tool_calls"]:
                        if not isinstance(tc, dict):
                            normalized_tcs.append(tc)
                            continue
                        # Deep copy the tool call dict and its nested function dict
                        tc_copy = dict(tc)
                        if isinstance(tc_copy.get("function"), dict):
                            tc_copy["function"] = dict(tc_copy["function"])
                        if tc_copy.get("id"):
                            tc_copy["id"] = normalize_tool_call_id(tc_copy["id"], normalize_strategy)
                        normalized_tcs.append(tc_copy)
                    m2["tool_calls"] = normalized_tcs

            out.append(m2)

        # As a final safety net, enforce the OpenAI tools protocol
        # invariant that every assistant message with ``tool_calls`` is
        # immediately followed by tool-role messages responding to each
        # ``tool_call_id``.  If history ever violated this (for example
        # due to buggy older versions of the UI), we drop the offending
        # assistant/tool messages from the provider view so that new
        # turns can proceed instead of failing with a persistent
        # "tool_calls must be followed by tool messages" error.
        return self._enforce_assistant_toolcall_protocol(out)


    def _enforce_assistant_toolcall_protocol(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensure assistant tool_calls are followed by matching tool messages.

        Provider APIs such as OpenAI require that an assistant message
        with ``tool_calls`` is immediately followed by one or more
        ``tool`` messages, each bearing a ``tool_call_id`` from that
        assistant turn.  If other roles (user/system/assistant) are
        interleaved before all tool_call_ids have received a response,
        the provider will reject the request.

        This helper walks the final, sanitized message list and:

          * Keeps assistant tool_call turns whose immediately-following
            tool messages form a complete, one-to-one response set for
            their ``tool_call_id`` values.
          * Drops assistant tool_call messages that do *not* have such
            a contiguous tool-message block, and also drops any tool
            messages whose ``tool_call_id`` does not belong to a kept
            assistant turn.

        The local event log remains untouched; this only affects what is
        sent to the provider, allowing previously "broken" threads to
        recover while preserving a faithful transcript for UIs.
        """

        if not messages:
            return messages

        n = len(messages)

        # First pass: determine which assistant tool_call messages have
        # a valid, contiguous block of tool responses immediately
        # following them, and record the set of tool_call_ids that
        # belong to such "good" turns.
        good_assistant_idx = set()
        good_tool_ids: set[str] = set()

        i = 0
        while i < n:
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "assistant":
                tcs = m.get("tool_calls") or []
                if isinstance(tcs, list) and tcs:
                    expected_ids: list[str] = []
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        # OpenAI-style tool calls: {"id": "...", "function": {...}}
                        tcid = tc.get("id") or tc.get("tool_call_id")
                        if not tcid and isinstance(tc.get("function"), dict):
                            tcid = tc["function"].get("id")
                        if isinstance(tcid, str) and tcid:
                            expected_ids.append(tcid)
                    if expected_ids:
                        remaining = set(expected_ids)
                        j = i + 1
                        ok = True
                        # Consume a contiguous block of tool messages
                        # immediately following this assistant. Any
                        # non-tool or unexpected tool_call_id breaks the
                        # adjacency requirement.
                        while j < n:
                            mj = messages[j]
                            if not isinstance(mj, dict) or mj.get("role") != "tool":
                                break
                            tcid2 = mj.get("tool_call_id")
                            if not isinstance(tcid2, str) or not tcid2:
                                ok = False
                                break
                            if tcid2 not in remaining:
                                # Either duplicate or unmatched id;
                                # treat this assistant turn as
                                # malformed for provider purposes.
                                ok = False
                                break
                            remaining.remove(tcid2)
                            j += 1
                        if ok and not remaining:
                            good_assistant_idx.add(i)
                            for tid in expected_ids:
                                good_tool_ids.add(tid)
                        # Skip over the tool block we just inspected
                        i = j
                        continue
            i += 1

        # Second pass: rebuild messages, keeping only:
        #   - non-tool, non-assistant-tool_call messages as-is
        #   - assistant tool_call messages whose index is in
        #     good_assistant_idx, plus their immediately-following tool
        #     messages for good_tool_ids
        #   - tool messages whose tool_call_id is in good_tool_ids and
        #     which are not part of a malformed assistant turn.
        out: List[Dict[str, Any]] = []
        i = 0
        while i < n:
            m = messages[i]
            if not isinstance(m, dict):
                out.append(m)
                i += 1
                continue

            role = m.get("role")

            if i in good_assistant_idx:
                # Keep the assistant tool_call message and its
                # contiguous block of tool responses that we
                # already validated above.
                out.append(m)
                i += 1
                while i < n:
                    mj = messages[i]
                    if not isinstance(mj, dict) or mj.get("role") != "tool":
                        break
                    tcid2 = mj.get("tool_call_id")
                    if not isinstance(tcid2, str) or tcid2 not in good_tool_ids:
                        break
                    out.append(mj)
                    i += 1
                continue

            if role == "assistant":
                tcs = m.get("tool_calls") or []
                if isinstance(tcs, list) and tcs:
                    # Malformed assistant tool_call turn: drop the
                    # assistant message itself and skip over any
                    # immediately-following tool messages that belong
                    # to its declared tool_call_ids. Any remaining
                    # orphan tool messages for these ids will be
                    # discarded by the generic tool-handling block
                    # below.
                    bad_ids: set[str] = set()
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        tcid = tc.get("id") or tc.get("tool_call_id")
                        if not tcid and isinstance(tc.get("function"), dict):
                            tcid = tc["function"].get("id")
                        if isinstance(tcid, str) and tcid:
                            bad_ids.add(tcid)
                    i += 1
                    while i < n:
                        mj = messages[i]
                        if not isinstance(mj, dict) or mj.get("role") != "tool":
                            break
                        tcid2 = mj.get("tool_call_id")
                        if isinstance(tcid2, str) and tcid2 in bad_ids:
                            # Skip tool message that belongs to this
                            # malformed assistant turn.
                            i += 1
                            continue
                        break
                    continue

            if role == "tool":
                tcid = m.get("tool_call_id")
                if isinstance(tcid, str) and tcid in good_tool_ids:
                    out.append(m)
                # Else: orphan or malformed tool message -> drop from
                # provider view.
                i += 1
                continue

            # All other messages (user/system/assistant without
            # tool_calls) are passed through unchanged.
            out.append(m)
            i += 1

        return out


    def _tool_stream_context(self, *, tc: ToolCallState, invoke_id: str, current_model: Optional[str]) -> ToolStreamContext:
        """Build live streaming hooks for a ToolRegistry execution."""
        stream_limiter = ToolStreamPreviewLimiter()
        suppressed_counter = {'count': 0}
        chunk_seq = self.db.max_chunk_seq(invoke_id)

        def _next_chunk_seq() -> int:
            nonlocal chunk_seq
            chunk_seq += 1
            return chunk_seq

        def _heartbeat() -> bool:
            return self.db.heartbeat(
                self.thread_id,
                invoke_id,
                _now_plus(self.cfg.lease_ttl_sec),
            )

        def _emit_delta(text: str) -> bool:
            try:
                text = self._filter_tool_output(text, mask_secrets=False)
            except Exception:
                pass
            return emit_limited_tool_stream_delta(
                self.db,
                stream_limiter,
                text,
                thread_id=self.thread_id,
                invoke_id=invoke_id,
                tool_call_id=tc.tool_call_id,
                tool_name=tc.name or '',
                current_model=current_model,
                heartbeat=_heartbeat,
                suppressed_counter=suppressed_counter,
                next_chunk_seq=_next_chunk_seq,
            )

        def _emit_summary(summary: str) -> None:
            emit_tool_summary_event(
                self.db,
                thread_id=self.thread_id,
                invoke_id=invoke_id,
                tool_call_id=tc.tool_call_id,
                tool_name=tc.name or '',
                summary=summary,
            )

        return ToolStreamContext(
            db=self.db,
            thread_id=self.thread_id,
            invoke_id=invoke_id,
            tool_call_id=tc.tool_call_id,
            tool_name=tc.name or '',
            current_model=current_model,
            heartbeat=_heartbeat,
            emit_delta=_emit_delta,
            emit_summary=_emit_summary,
        )

    async def _run_ra_tools(self, invoke_id: str, current_model: Optional[str], ra: RunnerActionable) -> None:
        """Handle RA2/RA3: process tool calls that are already approved or denied
        (TC2.1/TC2.2/TC5) and advance them along the state machine."""
        tool_calls = ra.tool_calls or []
        # Thread-level tools configuration (disables, etc.) is respected
        # both for assistant-originated tool calls (RA2) and
        # user-initiated ones (RA3).
        tools_cfg = get_thread_tools_config(self.db, self.thread_id)
        for tc in tool_calls:
            # Denied -> publish denial message and move to TC6
            if tc.state == 'TC2.2' and not tc.published:
                reason = 'Tool call execution denied.'
                msg = {
                    'role': 'tool',
                    'content': f"Tool call execution denied! Reason: {reason}",
                    'tool_call_id': tc.tool_call_id,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                if current_model:
                    msg['model_key'] = current_model
                try:
                    from .token_count import tool_message_tps_for_call
                    tps = tool_message_tps_for_call(
                        self.db,
                        self.thread_id,
                        str(tc.tool_call_id),
                        content=str(msg.get('content') or ''),
                    )
                    if isinstance(tps, float) and tps > 0:
                        msg['tps'] = tps
                except Exception:
                    pass
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=msg,
                )
                continue

            # Approved, not yet executed -> execution_started -> finished
            if tc.state == 'TC2.1':
                # Respect per-thread tool capabilities: instead of
                # executing the tool, immediately mark it finished with
                # a synthetic "not allowed" output. This applies equally
                # to assistant- and user-originated calls.
                if not tools_cfg.is_tool_allowed(tc.name):
                    import os as _os
                    disabled_msg = (
                        f"Tool '{tc.name}' is not allowed for this thread and "
                        "was not executed."
                    )
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='tool_call.finished',
                        msg_id=None,
                        invoke_id=invoke_id,
                        payload={
                            'tool_call_id': tc.tool_call_id,
                            'reason': 'disabled',
                            'output': disabled_msg,
                        },
                    )
                    # Immediately approve the small synthetic output so
                    # it can be published as a tool message on the next
                    # RA2/RA3 pass without user interaction.
                    self.db.append_event(
                        event_id=_os.urandom(10).hex(),
                        thread_id=self.thread_id,
                        type_='tool_call.output_approval',
                        msg_id=None,
                        invoke_id=None,
                        payload={
                            'tool_call_id': tc.tool_call_id,
                            'decision': 'whole',
                            'reason': 'Auto: tool not allowed for this thread',
                            'preview': disabled_msg,
                        },
                    )
                    continue

                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.execution_started',
                    msg_id=None,
                    invoke_id=invoke_id,
                    payload={'tool_call_id': tc.tool_call_id},
                )
                # ToolRegistry supports both sync and async tool
                # implementations. Synchronous tools run in a worker thread;
                # async tools are awaited directly.
                # Shared timeout resolution: LLM-specified > config > global default.
                tool_timeout_sec = resolve_tool_timeout_sec(
                    tc.arguments,
                    self.cfg.tool_timeout_sec if self.cfg is not None else None,
                    _default_tool_timeout_sec,
                )

                summary_stop = asyncio.Event()

                async def _summary_watcher() -> None:
                    if tool_timeout_sec is None:
                        return
                    start = time.time()
                    last = 0.0
                    while not summary_stop.is_set():
                        now = time.time()
                        if not last or now - last >= 1.0:
                            last = now
                            summary = tool_timeout_summary(tc.name or 'tool', tool_timeout_sec, start, now=now)
                            if summary:
                                try:
                                    emit_tool_summary_event(
                                        self.db,
                                        thread_id=self.thread_id,
                                        invoke_id=invoke_id,
                                        tool_call_id=tc.tool_call_id,
                                        tool_name=tc.name or '',
                                        summary=summary,
                                    )
                                except Exception:
                                    pass
                        try:
                            await asyncio.wait_for(summary_stop.wait(), timeout=0.25)
                        except asyncio.TimeoutError:
                            pass

                # Create a cancel check that returns True if lease is lost (e.g., Ctrl+C)
                def make_cancel_check(db_path, thread_id, invoke_id):
                    # Thread-local storage for executor thread's own connection
                    import threading
                    local = threading.local()

                    def check():
                        try:
                            # Create a fresh connection in the executor thread if needed.
                            # SQLite connections cannot be shared between threads.
                            if not hasattr(local, 'conn') or local.conn is None:
                                import sqlite3
                                local.conn = sqlite3.connect(str(db_path), timeout=5)
                            row = local.conn.execute(
                                "SELECT 1 FROM open_streams WHERE thread_id=? AND invoke_id=?",
                                (thread_id, invoke_id)
                            ).fetchone()
                            return row is None  # True = cancelled (lease lost)
                        except Exception:
                            return False  # If we can't check, assume not cancelled
                    return check

                cancel_check = make_cancel_check(self.db.path, self.thread_id, invoke_id)
                stream_ctx = self._tool_stream_context(tc=tc, invoke_id=invoke_id, current_model=current_model)
                summary_task = asyncio.create_task(_summary_watcher())

                try:
                    tool_result = await self.tools.execute_async(
                        tc.name,
                        tc.arguments,
                        thread_id=self.thread_id,
                        invoke_id=invoke_id,
                        origin='runner',
                        initial_model_key=current_model,
                        tool_timeout_sec=tool_timeout_sec,
                        cancel_check=cancel_check,
                        db=self.db,
                        stream=stream_ctx,
                        preserve_tool_result=True,
                    )
                    if isinstance(tool_result, ToolExecutionResult):
                        full_result = tool_result.output
                        finish_reason = tool_result.reason or 'success'
                        already_streamed = tool_result.streamed
                    else:
                        full_result = tool_result
                        finish_reason = 'success'
                        already_streamed = False
                except Exception as e:
                    full_result = f"ERROR: {e}"
                    finish_reason = 'error'
                    already_streamed = False
                finally:
                    summary_stop.set()
                    try:
                        await summary_task
                    except Exception:
                        pass
                if not isinstance(full_result, str):
                    full_result = str(full_result)

                # We intentionally do not mask secrets in the stored tool
                # output or the live UI stream. Secrets are only prevented
                # from reaching the provider API in _sanitize_messages_for_api().
                # However, always sanitize control characters before
                # streaming to avoid terminal escape issues.
                try:
                    full_result = self._filter_tool_output(full_result, mask_secrets=False)
                except Exception:
                    pass
                out = full_result or ''
                CH = 400
                cancelled = False
                if not already_streamed:
                    stream_limiter = ToolStreamPreviewLimiter()
                    suppressed_counter = {'count': 0}
                    chunk_seq = self.db.max_chunk_seq(invoke_id)

                    def _next_chunk_seq() -> int:
                        nonlocal chunk_seq
                        chunk_seq += 1
                        return chunk_seq

                    for i in range(0, len(out), CH):
                        part = out[i : i + CH]
                        # Respect the per-thread lease while emitting only a
                        # bounded live preview. The full output remains in
                        # full_result and is handled by the normal long-output
                        # approval/stash path below.
                        def _heartbeat() -> bool:
                            return self.db.heartbeat(
                                self.thread_id,
                                invoke_id,
                                _now_plus(self.cfg.lease_ttl_sec),
                            )

                        ok = emit_limited_tool_stream_delta(
                            self.db,
                            stream_limiter,
                            part,
                            thread_id=self.thread_id,
                            invoke_id=invoke_id,
                            tool_call_id=tc.tool_call_id,
                            tool_name=tc.name or '',
                            current_model=current_model,
                            heartbeat=_heartbeat,
                            suppressed_counter=suppressed_counter,
                            next_chunk_seq=_next_chunk_seq,
                        )
                        if not ok:
                            cancelled = True
                            break
                        await asyncio.sleep(0)
                if cancelled and finish_reason == 'success':
                    finish_reason = 'interrupted'
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='tool_call.finished',
                    msg_id=None,
                    invoke_id=invoke_id,
                    payload={
                        'tool_call_id': tc.tool_call_id,
                        'reason': finish_reason,
                        'output': full_result,
                    },
                )
                # Auto output-approval: small outputs get decision='whole'
                # and go through verbatim; long outputs are stashed to
                # disk and get decision='partial' with a preview that
                # references the saved file so the LLM can fetch the
                # full content on demand. A UI cancellation (Ctrl+C)
                # that already recorded an explicit decision is respected.
                try:
                    _emit_auto_output_approval(self.db, self.thread_id, tc.tool_call_id, full_result)
                except Exception:
                    pass

            # Output approval done (TC5) -> publish final tool message based on
            # the last tool_call.output_approval payload.
            if tc.state == 'TC5':
                payload = tc.last_output_approval_payload or {}
                decision = payload.get('decision')
                preview = payload.get('preview') or ''
                finished_output = tc.finished_output or ''
                finished_reason = (tc.finished_reason or '').lower()

                # Determine base content. For interrupted tool calls we want
                # to surface the partial output that was produced before the
                # interruption so the user can inspect it (even if the
                # decision is "omit" for LLM context).
                if finished_reason == 'interrupted':
                    # Prefer an explicit preview when decision='partial';
                    # otherwise fall back to a bounded preview of the partial
                    # output. Never publish the full finished_output into the
                    # tool message: published tool messages are eligible for
                    # provider context unless no_api, and interrupted tools can
                    # still produce very large partial output.
                    if decision == 'partial' and preview:
                        content = str(preview)
                    elif finished_output:
                        try:
                            content, _saved = stash_tool_output_and_build_preview(
                                self.db,
                                self.thread_id,
                                str(tc.tool_call_id),
                                finished_output,
                            )
                        except Exception:
                            content = finished_output[:PREVIEW_MAX_CHARS]
                            if len(finished_output) > PREVIEW_MAX_CHARS:
                                content = content.rstrip() + "\n\n...[output truncated for preview]..."
                    else:
                        content = str(preview or "Output omitted.")
                    # Append a clear note so it is obvious this output is
                    # incomplete.
                    note = "Output incomplete - interrupted"
                    if content:
                        if not content.rstrip().endswith(note):
                            content = content.rstrip() + "\n\n" + note
                    else:
                        content = note
                else:
                    # Non-interrupted calls keep the previous semantics.
                    if decision == 'omit':
                        # User chose to omit the (possibly huge) output; we keep a
                        # small placeholder string instead of the real content.
                        content = "Output omitted."
                    else:
                        # 'whole' or 'partial' (or unknown) -> use the preview string.
                        content = str(preview)

                # For user-originated commands ($ / $$), prepend the original
                # command text so that the message containing the output also
                # includes the command itself.
                if ra.kind == 'RA3_tools_user':
                    cmd_text = self._get_parent_message_content(tc.parent_msg_id)
                    if not cmd_text:
                        cmd_text = self._render_tool_invocation(tc)
                    if self._user_tool_call_wants_raw_result(tc):
                        cmd_text = None
                    if cmd_text:
                        # Avoid duplicating the command if the preview already starts with it.
                        if not content.startswith(cmd_text):
                            content = f"{cmd_text}\n\n{content}" if content else cmd_text

                # no_api rules:
                #  - For user-initiated commands (RA3), the model should not
                #    see this tool message at all when either the decision is
                #    "omit" *or* the parent user message was marked no_api
                #    (hidden "$$" commands). Visible "$" commands only hide
                #    the output when the decision is "omit".
                parent_no_api = self._parent_msg_has_no_api(tc.parent_msg_id) if ra.kind == 'RA3_tools_user' else False
                preserve_turn_answer_tool = bool(ra.kind == 'RA2_tools_assistant' and tc.name == ANSWER_USER_PRESERVE_TURN_TOOL_NAME)
                no_api_flag = bool(
                    (ra.kind == 'RA3_tools_user' and (decision == 'omit' or parent_no_api))
                    or preserve_turn_answer_tool
                )

                msg = {
                    'role': 'tool',
                    'content': content,
                    'tool_call_id': tc.tool_call_id,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                # For user-initiated commands (RA3), keep the user turn
                # after publishing the tool result. The model should not
                # be invoked automatically; instead, the result becomes
                # part of the context for the *next* user message.
                if ra.kind == 'RA3_tools_user' or preserve_turn_answer_tool:
                    msg['keep_user_turn'] = True
                if no_api_flag:
                    msg['no_api'] = True
                if current_model:
                    msg['model_key'] = current_model
                try:
                    from .token_count import tool_message_tps_for_call
                    tps = tool_message_tps_for_call(
                        self.db,
                        self.thread_id,
                        str(tc.tool_call_id),
                        content=str(msg.get('content') or ''),
                    )
                    if isinstance(tps, float) and tps > 0:
                        msg['tps'] = tps
                except Exception:
                    pass
                self.db.append_event(
                    event_id=os.urandom(10).hex(),
                    thread_id=self.thread_id,
                    type_='msg.create',
                    msg_id=os.urandom(10).hex(),
                    payload=msg,
                )

    def _get_parent_message_content(self, msg_id: str) -> Optional[str]:
        """Best-effort lookup of the original message content for a tool call.

        This is used primarily for user-initiated commands so that the
        final message containing the tool output also includes the
        original command text for readability.
        """
        if not msg_id:
            return None
        try:
            cur = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE msg_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
                (msg_id,),
            )
            row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            payload = {}
        content = payload.get('content')
        return content if isinstance(content, str) else None

    def _parent_msg_has_no_api(self, msg_id: str) -> bool:
        """Check whether the parent message for a tool call was tagged no_api.

        This is used to propagate the hidden semantics of "$$" user
        commands to their eventual tool result messages so that the
        provider never sees them.
        """
        if not msg_id:
            return False
        try:
            cur = self.db.conn.execute(
                "SELECT payload_json FROM events WHERE msg_id=? AND type='msg.create' ORDER BY event_seq DESC LIMIT 1",
                (msg_id,),
            )
            row = cur.fetchone()
        except Exception:
            return False
        if not row:
            return False
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            payload = {}
        return bool(payload.get('no_api'))

    def _user_tool_call_wants_raw_result(self, tc: ToolCallState) -> bool:
        """Return True for internal RA3 calls whose result is consumed by code.

        UI-originated user commands intentionally include the command text in
        the published tool message for readability.  REPL bridge helpers such
        as ``eggtools.spawn_agent()`` need the returned value to remain a raw
        thread id so user Python can immediately pass it to ``eggtools.wait``.
        """

        try:
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except Exception:
                    args = {}
            if isinstance(args, dict) and args.get('_egg_raw_thread_id_result'):
                return True
        except Exception:
            return False
        return False

    def _render_tool_invocation(self, tc: ToolCallState) -> str:
        """Render a human-readable representation of a tool invocation.

        Used as a fallback when we cannot recover the user's original
        command text (e.g. if the parent message was edited or missing).
        """
        try:
            args = tc.arguments
            if isinstance(args, str):
                try:
                    args_obj = json.loads(args) if args.strip() else {}
                except Exception:
                    args_obj = {"_raw": args}
            elif isinstance(args, dict):
                args_obj = args
            else:
                args_obj = {"_arg": args}
            if tc.name in ("bash", "python") and isinstance(args_obj.get("script"), str):
                script = args_obj.get("script")
                if tc.name == "bash":
                    return f"$ {script}"
                return f"python {script}"
            # Generic fallback
            return f"{tc.name}({json.dumps(args_obj, ensure_ascii=False)})"
        except Exception:
            return ""


    def _mask_secrets_heuristic(self, text: str) -> str:
        """Fast, heuristic masking for secret-like substrings.

        This is intended for UI streaming or as a cheap first-pass before
        running heavier secret scanners.

        It is deliberately conservative (may over-mask).
        """
        import re as _re

        if not isinstance(text, str) or not text:
            return text

        # 1) .env-style assignments (KEY=..., TOKEN=..., etc.)
        # Preserve quotes when present.
        #
        # We also treat some *_ID variables as potentially sensitive, but
        # we do so conservatively to avoid masking harmless short ids.
        def _mask_env_line(m: "_re.Match[str]") -> str:
            lead = m.group(1) or ""
            name = (m.group(2) or "").strip()
            sep = m.group(3) or "="
            val = m.group(4) or ""
            val = val.strip()

            # If the name only matches because of "ID" (and not because it
            # contains other secret-like keywords), only mask when the value
            # looks high-entropy / secret-ish.
            if name and 'ID' in name:
                strong_keywords = (
                    'KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASS', 'PRIVATE', 'CREDENTIAL'
                )
                if not any(k in name for k in strong_keywords):
                    looks_secret = False
                    # long token-ish
                    if len(val) >= 24:
                        looks_secret = True
                    # UUID
                    if _re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", val.lower()):
                        looks_secret = True
                    # long hex
                    if _re.search(r"[A-Fa-f0-9]{16,}", val):
                        looks_secret = True
                    if not looks_secret:
                        return lead + name + sep + val

            if len(val) <= 1:
                return lead + name + sep + "***"
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                q = val[0]
                return lead + name + sep + q + "***" + q
            return lead + name + sep + "***"

        text = _re.sub(
            # Also match commented-out env lines, e.g.
            #   #API_KEY=...
            #   # export API_KEY=...
            #   #export API_KEY=...
            r"(?im)^(\s*(?:#\s*)?(?:export\s+)?)([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PRIVATE|CREDENTIAL|ID)[A-Z0-9_]*)(\s*=\s*)([^\r\n]+)$",
            _mask_env_line,
            text,
        )

        # 2) Authorization headers / bearer tokens
        text = _re.sub(r"(?i)(Authorization\s*:\s*Bearer\s+)([^\s\r\n]+)", r"\1***", text)
        text = _re.sub(r"(?i)(Bearer\s+)([^\s\r\n]+)", r"\1***", text)

        # 3) Common API token formats
        replacements = [
            (r"\bsk-[A-Za-z0-9]{20,}\b", "sk-***"),
            (r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b", "sk-ant-***"),
            (r"\bghp_[A-Za-z0-9]{20,}\b", "ghp_***"),
            (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "github_pat_***"),
            (r"\bhf_[A-Za-z0-9]{20,}\b", "hf_***"),
            (r"\bAKIA[0-9A-Z]{16}\b", "AKIA***"),
            (r"\bAIza[0-9A-Za-z\-_]{20,}\b", "AIza***"),
        ]
        for pat, rep in replacements:
            text = _re.sub(pat, rep, text)

        return text


    def _filter_tool_output(self, text: str, *, mask_secrets: bool = True) -> str:
        """Filter raw tool output before it is persisted or displayed.

        This performs two classes of filtering:

          1. Sanitize control characters that frequently confuse
             terminal emulators (e.g. stray escape sequences, other
             non-printables) while preserving newlines and
             tabs. Characters outside a conservative printable set are
             replaced with the Unicode replacement character.

          2. Best-effort masking of secret-like values using the
             optional ``detect-secrets`` library. When available, we
             run the built-in plugins against the output and mask the
             span of each detected secret with ``"***"``. When the
             library is not installed, we simply return the sanitized
             text as-is.

        Secret masking can be disabled (e.g. via a UI flag) by calling
        this helper with ``mask_secrets=False``. Control-character
        sanitization is always applied to protect the terminal.
        """

        if not isinstance(text, str) or not text:
            return text

        cleaned = sanitize_terminal_text(text)

        # 2) Secret detection and masking (best-effort, optional dep).
        if not mask_secrets:
            return cleaned

        try:
            import importlib.util as _importlib_util

            if not _importlib_util.find_spec('detect_secrets'):
                try:
                    cleaned = self._mask_secrets_heuristic(cleaned)
                except Exception:
                    pass
                return cleaned

            from detect_secrets import SecretsCollection  # type: ignore
            from detect_secrets.settings import default_settings  # type: ignore

            # We scan the output as a single in-memory "file".  The
            # detector API expects bytes and an associated filename.
            with default_settings():
                sc = SecretsCollection()
                # The scan method on SecretsCollection normally works
                # on files; for in-memory text we can use the
                # ``scan_lines`` helper available on individual
                # plugins. For portability across detect-secrets
                # versions, we instead call ``scan_file`` via a
                # temporary NamedTemporaryFile when necessary.

                import tempfile as _tempfile, os as _os

                with _tempfile.NamedTemporaryFile('w+', delete=False, encoding='utf-8') as tmp:
                    tmp.write(cleaned)
                    tmp.flush()
                    tmp_path = tmp.name

                try:
                    sc.scan_file(tmp_path)
                finally:
                    try:
                        _os.unlink(tmp_path)
                    except Exception:
                        pass

                if not sc.data:
                    return cleaned

                # Mask all detected secrets by character span.  We
                # re-open the temp file contents to compute spans.
                # Since we already have ``cleaned`` in memory, we
                # simply operate on that string.
                secrets_for_file = next(iter(sc.data.values()), [])
                if not secrets_for_file:
                    return cleaned

                # Build a list of (start, end) index ranges to mask.
                # We intentionally avoid including trailing newline
                # characters in the span so we do not accidentally
                # join lines when masking.
                spans: list[tuple[int, int]] = []
                for sec in secrets_for_file:
                    try:
                        # Each ``sec`` has line_number and secret_hash
                        # but not the raw value; we conservatively mask
                        # the full line containing the secret.
                        line_no = int(getattr(sec, 'line_number', 0) or 0)
                    except Exception:
                        line_no = 0
                    if line_no <= 0:
                        continue
                    lines = cleaned.splitlines(keepends=True)
                    if 1 <= line_no <= len(lines):
                        line_txt = lines[line_no - 1]
                        start = sum(len(l) for l in lines[: line_no - 1])
                        # Exclude common line terminators from masking
                        # so the output formatting is preserved.
                        trim = line_txt.rstrip('\r\n')
                        end = start + len(trim)
                        if end > start:
                            spans.append((start, end))

                if not spans:
                    return cleaned

                # Merge overlapping spans and build masked string
                spans.sort()
                merged: list[tuple[int, int]] = []
                cur_start, cur_end = spans[0]
                for s, e in spans[1:]:
                    if s <= cur_end:
                        cur_end = max(cur_end, e)
                    else:
                        merged.append((cur_start, cur_end))
                        cur_start, cur_end = s, e
                merged.append((cur_start, cur_end))

                out_parts: list[str] = []
                last = 0
                mask = '<MASKED SECRET with detect-secrets>'
                for s, e in merged:
                    if last < s:
                        out_parts.append(cleaned[last:s])
                    out_parts.append(mask)
                    last = e
                if last < len(cleaned):
                    out_parts.append(cleaned[last:])
                return ''.join(out_parts)

        except Exception:
            # If anything goes wrong with detect-secrets integration,
            # fall back to the control-char-sanitised version.
            return cleaned

        return cleaned


def _is_thread_idle(db: ThreadsDB, thread_id: str) -> bool:
    """Check if a thread is idle (waiting for user input/action).

    Idle = NOT runnable AND no open stream
    NOT idle = runnable OR has open stream (waiting for API)
    """
    from .api import is_thread_runnable

    # Not idle if runnable
    if is_thread_runnable(db, thread_id):
        return False

    # Not idle if waiting for API response (has open stream)
    row = db.conn.execute(
        "SELECT 1 FROM open_streams WHERE thread_id = ? LIMIT 1",
        (thread_id,)
    ).fetchone()
    if row:
        return False

    return True


def _sort_by_priority(threads: List[str], mode: str, db: ThreadsDB) -> List[str]:
    """Sort threads by priority (desc), with mode as tie-breaker for equal priorities."""
    from .api import get_thread_scheduling

    # Get priority for each thread (default 0)
    threads_with_priority = [(tid, get_thread_scheduling(db, tid).priority) for tid in threads]

    if mode == "alphabetical":
        # Sort by priority descending, then by thread_id ascending for ties
        threads_with_priority.sort(key=lambda x: (-x[1], x[0]))
    else:
        # "none": Sort by priority descending, preserve original order for ties (stable sort)
        threads_with_priority.sort(key=lambda x: -x[1])

    return [tid for tid, _ in threads_with_priority]


def _thread_id_batches(thread_ids: List[str], batch_size: int = 900):
    for idx in range(0, len(thread_ids), batch_size):
        yield thread_ids[idx:idx + batch_size]


def _max_event_seqs_bulk(db: ThreadsDB, thread_ids: List[str]) -> Dict[str, int]:
    """Return max event sequence per thread with batched index seeks."""
    out = {tid: -1 for tid in thread_ids}
    for batch in _thread_id_batches(thread_ids):
        if not batch:
            continue
        values = ",".join("(?)" for _ in batch)
        try:
            cur = db.conn.execute(
                f"""
                WITH wanted(thread_id) AS (VALUES {values})
                SELECT w.thread_id,
                       (
                           SELECT MAX(e.event_seq)
                           FROM events AS e INDEXED BY events_thread_seq
                           WHERE e.thread_id = w.thread_id
                       )
                FROM wanted AS w
                """,
                tuple(batch),
            )
            for row in cur.fetchall():
                value = row[1]
                out[str(row[0])] = int(value) if value is not None else -1
        except Exception:
            continue
    return out


def _active_open_threads_bulk(db: ThreadsDB, thread_ids: List[str]) -> Set[str]:
    """Return thread ids with currently active leases."""
    out: Set[str] = set()
    now_iso = _utcnow_iso()
    for batch in _thread_id_batches(thread_ids):
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        try:
            cur = db.conn.execute(
                f"SELECT thread_id FROM open_streams WHERE lease_until > ? AND thread_id IN ({placeholders})",
                (now_iso, *batch),
            )
            out.update(str(row[0]) for row in cur.fetchall())
        except Exception:
            continue
    return out


def _thread_scheduling_bulk(db: ThreadsDB, thread_ids: List[str]) -> Dict[str, _SchedulerThreadSettings]:
    """Return latest scheduler priority/threshold settings for threads."""
    out = {tid: _SchedulerThreadSettings() for tid in thread_ids}
    for batch in _thread_id_batches(thread_ids):
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        try:
            cur = db.conn.execute(
                f"""
                SELECT e.thread_id, e.payload_json
                  FROM events e
                  JOIN (
                        SELECT thread_id, MAX(event_seq) AS event_seq
                          FROM events
                         WHERE type='thread.scheduling'
                           AND thread_id IN ({placeholders})
                         GROUP BY thread_id
                       ) latest
                    ON latest.thread_id = e.thread_id
                   AND latest.event_seq = e.event_seq
                """,
                tuple(batch),
            )
            for row in cur.fetchall():
                try:
                    payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                out[str(row[0])] = _SchedulerThreadSettings(
                    priority=payload.get("priority", 0),
                    threshold=payload.get("threshold"),
                )
        except Exception:
            continue
    return out


def _sort_by_priority_map(
    threads: List[str],
    mode: str,
    settings_by_thread: Dict[str, _SchedulerThreadSettings],
) -> List[str]:
    """Sort threads using already-loaded scheduling settings."""
    threads_with_priority = [
        (tid, settings_by_thread.get(tid, _SchedulerThreadSettings()).priority or 0)
        for tid in threads
    ]
    if mode == "alphabetical":
        threads_with_priority.sort(key=lambda x: (-x[1], x[0]))
    else:
        threads_with_priority.sort(key=lambda x: -x[1])
    return [tid for tid, _ in threads_with_priority]


class SubtreeScheduler:
    """Async orchestrator: watches a root thread and runs runnable threads within its subtree, up to concurrency limit."""

    def __init__(self, db: ThreadsDB, root_thread_id: str, llm: Optional[LLMClient] = None, owner: Optional[str] = None, config: Optional[RunnerConfig] = None,
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None):
        self.db = db
        self.root = root_thread_id
        if llm is not None:
            self.llm = llm
        elif LLMClient is not None:
            self.llm = LLMClient(models_path=models_path or 'models.json', all_models_path=all_models_path or 'all-models.json')
        else:
            self.llm = None
        # Debug print — skip when stdout is a real terminal so interactive
        # TUIs (e.g. egg) don't see it above the live region. Piped /
        # captured runs (CLI logs, pytest) still get the diagnostic.
        import sys as _sys
        try:
            _is_tty = bool(_sys.stdout.isatty())
        except Exception:
            _is_tty = False
        if not _is_tty:
            print(f"LLMClient type: {type(self.llm)} module: {type(self.llm).__module__} has astream_chat: {hasattr(self.llm, 'astream_chat')}")
        self.owner = owner or os.environ.get("USER") or "scheduler"
        self.cfg = config or RunnerConfig()
        self.tools = tools or create_default_tools()
        self.models_path = models_path or 'models.json'
        self.all_models_path = all_models_path or 'all-models.json'
        self._tasks: Set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """Cancel and await runner tasks spawned by this scheduler."""
        tasks = list(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)

    def _collect_subtree(self, thread_id: str) -> List[str]:
        now_iso = _utcnow_iso()
        try:
            cur = self.db.conn.execute(
                """
                WITH RECURSIVE subtree(thread_id, depth, path) AS (
                    SELECT ? AS thread_id, 0 AS depth, ? AS path
                    UNION ALL
                    SELECT c.child_id, subtree.depth + 1, subtree.path || c.child_id || '/'
                      FROM children c
                      JOIN subtree ON c.parent_id = subtree.thread_id
                     WHERE (c.waiting_until IS NULL OR c.waiting_until <= ?)
                       AND instr(subtree.path, '/' || c.child_id || '/') = 0
                )
                SELECT thread_id FROM subtree ORDER BY depth, path
                """,
                (thread_id, f"/{thread_id}/", now_iso),
            )
            return [str(row[0]) for row in cur.fetchall()]
        except Exception:
            pass

        # Fallback BFS through children table.
        out: List[str] = []
        q: deque[str] = deque([thread_id])
        seen = set()
        while q:
            t = q.popleft()
            if t in seen:
                continue
            seen.add(t)
            # Respect waiting_until: only include children that are not waiting or waiting_until <= now
            out.append(t)
            cur = self.db.conn.execute("SELECT child_id, waiting_until FROM children WHERE parent_id=?", (t,))
            for row in cur.fetchall():
                wu = row["waiting_until"]
                if wu is None or wu <= now_iso:
                    q.append(row["child_id"])
        return out

    async def run_forever(self, poll_sec: float = 0.5):
        if _no_api_calls_mode(self.cfg):
            # This banner fires inside run_forever, which under an interactive
            # TUI (e.g. egg) runs after the renderer has taken over stdout —
            # a raw print there corrupts the viewport. Skip the print when
            # stdout is a real terminal; emit it only when output is piped
            # or captured (CLI / pytest capsys), which is when the message
            # is actually useful to see.
            import sys as _sys
            try:
                _is_tty = bool(_sys.stdout.isatty())
            except Exception:
                _is_tty = False
            if not _is_tty:
                print("[NO_API_CALLS] Read-only mode: RA1/RA2 disabled, only user commands allowed")

        # Track currently running threads to avoid creating duplicate tasks.
        # Values are scheduler resource classes: "llm" or "tool".  Tool
        # turns are still running/leased, but only "llm" consumes the
        # scarce provider concurrency budget.
        running_threads: Dict[str, str] = {}

        # Cheap per-thread event watermark to short-circuit expensive
        # runnable checks when a thread's event log has not changed
        # since the last iteration.
        last_checked_seq: Dict[str, int] = {}

        # Sticky scheduling state
        last_run_end: Dict[str, float] = {}  # thread_id -> monotonic time when last run ended
        reserved_slots: Set[str] = set()     # threads with reserved slots (recently ran, within threshold)

        async def drive(tid: str, resource_class: str):
            try:
                runner = ThreadRunner(
                    self.db,
                    tid,
                    llm=self.llm,
                    owner=self.owner,
                    purpose="assistant_stream",
                    config=self.cfg,
                    models_path=self.models_path,
                    all_models_path=self.all_models_path,
                    tools=self.tools,
                )
                try:
                    await runner.run_once()
                except Exception:
                    # Clear cache to force re-check on next iteration
                    last_checked_seq.pop(tid, None)
            finally:
                # Remove from running set when done
                running_threads.pop(tid, None)
                # Reserve slot (actual idle check happens in scheduling loop)
                if self.cfg.sticky_scheduling and resource_class == "llm":
                    reserved_slots.add(tid)

        while True:
            all_threads = self._collect_subtree(self.root)
            max_event_seqs = _max_event_seqs_bulk(self.db, all_threads)
            active_open_threads = _active_open_threads_bulk(self.db, all_threads)
            scheduling_settings = _thread_scheduling_bulk(self.db, all_threads)

            # Update idle tracking and expire reservations (sticky scheduling)
            if self.cfg.sticky_scheduling:
                now = time.monotonic()

                # For each reserved thread, check if it's truly idle
                for tid in list(reserved_slots):
                    if tid in running_threads:
                        # Running threads are not idle - clear any idle timer
                        last_run_end.pop(tid, None)
                        continue

                    if not _is_thread_idle(self.db, tid):
                        # Thread is waiting for API/tool approval - not idle, clear timer
                        last_run_end.pop(tid, None)
                        continue

                    # Thread is truly idle - start or continue idle timer
                    if tid not in last_run_end:
                        last_run_end[tid] = now  # Start idle timer

                # Expire reservations for threads idle too long
                expired: Set[str] = set()
                for tid in reserved_slots:
                    if tid not in last_run_end:
                        continue  # Not idle yet
                    # Use per-thread threshold if set, otherwise global default
                    settings = scheduling_settings.get(tid, _SchedulerThreadSettings())
                    threshold = settings.threshold if settings.threshold is not None else self.cfg.sticky_idle_threshold_sec
                    if (now - last_run_end[tid]) > threshold:
                        expired.add(tid)
                reserved_slots -= expired
                for tid in expired:
                    last_run_end.pop(tid, None)

            # Apply priority sorting
            all_threads = _sort_by_priority_map(all_threads, self.cfg.priority_mode, scheduling_settings)

            # Calculate available LLM/tool slots for scheduling.  Tool work
            # does not consume LLM slots; an optional separate tool cap can
            # be configured for backpressure, but defaults to unlimited.
            running_llm = sum(1 for kind in running_threads.values() if kind == "llm")
            running_tool = sum(1 for kind in running_threads.values() if kind == "tool")
            active_reserved_llm = sum(
                1 for tid in reserved_slots
                if tid not in running_threads
            ) if self.cfg.sticky_scheduling else 0
            llm_slots_used = running_llm + active_reserved_llm
            available_llm_slots = self.cfg.effective_max_concurrent_llm_threads - llm_slots_used
            if self.cfg.max_concurrent_tool_threads is None:
                available_tool_slots: Optional[int] = None
            else:
                available_tool_slots = int(self.cfg.max_concurrent_tool_threads) - running_tool

            # First pass: find runnable threads in priority order
            # We check all threads but only schedule up to available_slots
            runnable_candidates: List[tuple[str, RunnerActionable, str]] = []
            # Track max_seq for threads we check - only update last_checked_seq
            # for threads that are NOT runnable (truly idle) or that we schedule
            checked_seqs: Dict[str, int] = {}

            for tid in all_threads:
                if tid in running_threads:
                    continue

                # Quick cheap check: skip threads whose event log has
                # not changed since the last scheduler iteration.  This
                # avoids repeatedly running the relatively expensive
                # is_thread_runnable()/discover_runner_actionable logic
                # on completely idle threads.
                try:
                    max_seq = max_event_seqs.get(tid, -1)
                except Exception:
                    max_seq = -1
                if max_seq == last_checked_seq.get(tid, -1):
                    continue
                checked_seqs[tid] = max_seq

                # Skip threads with an active lease held by another process.
                # This prevents unnecessary is_thread_runnable() calls and
                # failed try_open_stream() attempts when TUI or another eggw
                # instance is already running the thread.
                # NOTE: We do NOT update watermark here - when lease expires,
                # we want to re-check the thread.
                if tid in active_open_threads:
                    continue

                ra = discover_runner_actionable_cached(self.db, tid)
                if ra is not None:
                    runnable_candidates.append((tid, ra, runner_actionable_resource_class(ra)))
                else:
                    # Thread is NOT runnable - mark as checked so we skip it
                    # until its events change
                    last_checked_seq[tid] = checked_seqs[tid]

            # Second pass: schedule runnable threads.  Only RA1/LLM work
            # consumes available_llm_slots; RA2/RA3 tool work is scheduled
            # independently (unless an explicit tool cap is configured).
            # (candidates are already in priority order from all_threads)
            for tid, _ra, resource_class in runnable_candidates:
                if resource_class == "llm" and available_llm_slots <= 0:
                    continue
                if resource_class == "tool" and available_tool_slots is not None and available_tool_slots <= 0:
                    continue

                # Check if we can schedule this thread (sticky scheduling)
                if self.cfg.sticky_scheduling and resource_class == "llm":
                    is_reserved = tid in reserved_slots
                    active_reserved = reserved_slots - set(running_threads.keys())
                    used_slots = running_llm + len(active_reserved)

                    if not is_reserved and used_slots >= self.cfg.effective_max_concurrent_llm_threads:
                        continue  # No LLM slots for non-reserved threads

                    reserved_slots.add(tid)
                    last_run_end.pop(tid, None)  # Clear idle timer when scheduled

                running_threads[tid] = resource_class
                task = asyncio.create_task(drive(tid, resource_class))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                if resource_class == "llm":
                    available_llm_slots -= 1
                elif available_tool_slots is not None:
                    available_tool_slots -= 1

            try:
                await asyncio.sleep(poll_sec)
            except asyncio.CancelledError:
                await self.shutdown()
                raise

