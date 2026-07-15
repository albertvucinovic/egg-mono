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
from .db import InvocationEventWriter, LeaseLost, ThreadsDB
from .tools import (
    ToolExecutionResult,
    ToolRegistry,
    ToolStreamContext,
    create_default_tools,
    resolve_tool_timeout_arg,
)
from .tool_state import (
    ToolCallState,
    RunnerActionable,
    discover_runner_actionable,
    discover_runner_actionable_cached,
    thread_state,
    build_tool_call_states,
    _prune_reducer_cache_for_threads,
)
from .tools_config import get_thread_tools_config
from .tool_output import (
    ToolOutputPersistenceError,
    ToolOutputPlanError,
    ToolOutputPublicationPlan,
    ToolOutputStateConflict,
    _artifact_is_ready,
    finalize_tool_output,
)
from .tool_call_id import normalize_tool_call_id
from .terminal_safety import sanitize_terminal_text
from .content_parts import content_has_artifacts, content_has_attachments, content_to_plain_text, validate_content_parts


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


def scheduler_task_status(task: Any) -> str:
    """Return a compact process-local scheduler task status string.

    The durable coordination mechanism for work is the per-thread SQLite
    lease, not a process-local scheduler registry. Egg/EggW registries are
    only convenience maps from a visited root to this process's resident
    asyncio task, so callers must be able to distinguish a genuinely live task
    from stale test objects, missing tasks, cancelled tasks, or crashed tasks.
    """

    if task is None:
        return "missing"

    done = getattr(task, "done", None)
    if not callable(done):
        return "unknown"

    try:
        is_done = bool(done())
    except Exception as e:
        return f"unknown: {type(e).__name__}: {e}"

    if not is_done:
        return "running"

    cancelled = getattr(task, "cancelled", None)
    try:
        if callable(cancelled) and bool(cancelled()):
            return "cancelled"
    except Exception:
        pass

    exception = getattr(task, "exception", None)
    if callable(exception):
        try:
            exc = exception()
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as e:
            return f"done: exception unavailable ({type(e).__name__}: {e})"
        if exc is not None:
            return f"failed: {type(exc).__name__}: {exc}"

    return "done"


def scheduler_task_is_live(task: Any) -> bool:
    """True only for an active in-process scheduler task."""

    return scheduler_task_status(task) == "running"


@dataclass(frozen=True)
class _SchedulerThreadSettings:
    priority: Any = 0
    threshold: Any = None


# SubtreeScheduler runs inside the TUI-owned asyncio loop.  Keep its
# synchronous bookkeeping cooperative without adding a public tuning knob.
_SCHEDULER_FAIRNESS_CHECKS_PER_YIELD = 16
_SCHEDULER_FAIRNESS_TIME_SLICE_SEC = 0.01


# Thresholds above which a tool output is considered "long" and should
# be stored as an artifact with a preview sent to the LLM instead of the full
# content. Matches the existing "prompt the user" thresholds so
# behaviour is continuous with prior versions.
LONG_OUTPUT_LINE_THRESHOLD = 800
LONG_OUTPUT_CHAR_THRESHOLD = 100_000
MAX_STORED_TOOL_OUTPUT_CHARS = 10_000_000
LONG_OUTPUT_CHUNK_LINES = 400
LONG_OUTPUT_CHUNK_CHARS = 40_000

# Size of the preview that gets embedded in the tool message when
# the full output is stored as an artifact.
PREVIEW_MAX_LINES = 200
PREVIEW_MAX_CHARS = 8000
TOOL_STREAM_PREVIEW_MAX_LINES = PREVIEW_MAX_LINES
TOOL_STREAM_PREVIEW_MAX_CHARS = PREVIEW_MAX_CHARS


class ToolStreamPreviewLimiter:
    """Bound live tool-output streaming to a short preview.

    Full tool output is still accumulated by the runner and later handled by
    the normal output-approval/artifact path. This helper only controls what is
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
        "Full output will be stored as a read_long_tool_output artifact if it exceeds the normal long-output threshold.]\n"
    )


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _capped_tool_output(full_output: str) -> tuple[str, bool, int]:
    if not isinstance(full_output, str):
        full_output = str(full_output or "")
    original_char_count = len(full_output)
    if original_char_count > MAX_STORED_TOOL_OUTPUT_CHARS:
        return full_output[:MAX_STORED_TOOL_OUTPUT_CHARS], True, original_char_count
    return full_output, False, original_char_count


def _tool_output_chunk_size_chars() -> int:
    return max(1, min(int(LONG_OUTPUT_CHUNK_CHARS), LONG_OUTPUT_CHAR_THRESHOLD // 2))


def _tool_output_chunk_size_lines() -> int:
    return max(1, min(int(LONG_OUTPUT_CHUNK_LINES), LONG_OUTPUT_LINE_THRESHOLD // 2))


def _split_tool_output_chunks(text: str) -> list[str]:
    chunk_size_chars = _tool_output_chunk_size_chars()
    chunk_size_lines = _tool_output_chunk_size_lines()
    if text == "":
        return [""]

    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    current_lines = 0
    i = 0
    n = len(text)
    while i < n:
        newline_at = text.find("\n", i)
        if newline_at == -1:
            segment = text[i:]
            i = n
        else:
            segment = text[i : newline_at + 1]
            i = newline_at + 1

        while segment:
            remaining_chars = chunk_size_chars - current_chars
            if remaining_chars <= 0 or current_lines >= chunk_size_lines:
                chunks.append("".join(current))
                current = []
                current_chars = 0
                current_lines = 0
                remaining_chars = chunk_size_chars

            part = segment[:remaining_chars]
            segment = segment[len(part):]
            current.append(part)
            current_chars += len(part)
            if part.endswith("\n"):
                current_lines += 1

            if current_chars >= chunk_size_chars or current_lines >= chunk_size_lines:
                chunks.append("".join(current))
                current = []
                current_chars = 0
                current_lines = 0

    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _tool_output_preview_text(full_output: str, *, max_lines: int, max_chars: int) -> str:
    lines = full_output.splitlines()
    preview = full_output
    if len(lines) > max_lines:
        preview = "\n".join(lines[:max_lines])
    if len(preview) > max_chars:
        preview = preview[:max_chars]
    return preview


def _stash_tool_output_artifact(
    thread_id: str,
    tool_call_id: str,
    full_output: str,
    *,
    original_char_count: Optional[int] = None,
    output_capped: bool = False,
) -> Dict[str, Any]:
    """Store tool output chunks and return artifact metadata.

    This is shared by long-output previewing and optimizer raw-output recovery.
    It never changes the canonical ``tool_call.finished.output`` event.
    """

    if not isinstance(full_output, str):
        full_output = str(full_output or "")

    stored_output, capped, detected_original_char_count = _capped_tool_output(full_output)
    if original_char_count is None:
        original_char_count = detected_original_char_count
    else:
        try:
            original_char_count = max(int(original_char_count), detected_original_char_count)
        except Exception:
            original_char_count = detected_original_char_count
    capped = bool(output_capped or capped or original_char_count > len(stored_output))
    chunks = _split_tool_output_chunks(stored_output)
    chunk_count = len(chunks)
    stored_char_count = len(stored_output)
    original_line_count = _line_count(full_output)
    stored_line_count = _line_count(stored_output)

    saved_path = ""
    artifact_id = ""
    try:
        workspace = Path.cwd().resolve()
        from .output_paths import create_thread_artifact_dir

        artifact_id, artifact_dir = create_thread_artifact_dir(workspace, thread_id)
        for index, chunk in enumerate(chunks, start=1):
            chunk_path = artifact_dir / f"chunk-{index:04d}.txt"
            chunk_path.write_text(chunk, encoding="utf-8")
            try:
                os.chmod(chunk_path, 0o600)
            except Exception:
                pass
        chunk_start_lines: list[int] = []
        next_source_line = 1
        for chunk in chunks:
            chunk_start_lines.append(next_source_line)
            next_source_line += chunk.count("\n")
        metadata = {
            "artifact_id": artifact_id,
            "thread_id": str(thread_id or ""),
            "tool_call_id": str(tool_call_id or ""),
            "chunk_count": chunk_count,
            "chunk_start_lines": chunk_start_lines,
            "chunk_size_chars": _tool_output_chunk_size_chars(),
            "chunk_size_lines": _tool_output_chunk_size_lines(),
            "stored_char_count": stored_char_count,
            "original_char_count": original_char_count,
            "stored_line_count": stored_line_count,
            "original_line_count": original_line_count,
            "capped": capped,
            "max_stored_chars": MAX_STORED_TOOL_OUTPUT_CHARS,
        }
        metadata_path = artifact_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        try:
            os.chmod(metadata_path, 0o600)
            os.chmod(artifact_dir, 0o700)
        except Exception:
            pass
        saved_path = str(artifact_dir)
    except Exception:
        saved_path = ""
        artifact_id = ""

    return {
        "stored_output": stored_output,
        "saved_path": saved_path,
        "artifact_id": artifact_id,
        "chunk_count": chunk_count,
        "stored_char_count": stored_char_count,
        "original_char_count": original_char_count,
        "stored_line_count": stored_line_count,
        "original_line_count": original_line_count,
        "capped": capped,
    }


def stash_tool_output_artifact_and_build_note(
    db,
    thread_id: str,
    tool_call_id: str,
    full_output: str,
    *,
    original_char_count: Optional[int] = None,
    output_capped: bool = False,
) -> tuple[str, str]:
    """Persist raw tool output as an artifact and return ``(note, saved_path)``.

    Optimized previews can append this note so the model/user can recover the
    original output through the existing ``read_long_tool_output`` tool.
    """

    artifact = _stash_tool_output_artifact(
        thread_id,
        tool_call_id,
        full_output,
        original_char_count=original_char_count,
        output_capped=output_capped,
    )
    artifact_id = str(artifact.get("artifact_id") or "")
    saved_path = str(artifact.get("saved_path") or "")
    if not artifact_id:
        return "", ""
    chunk_count = int(artifact.get("chunk_count") or 1)
    stored_char_count = int(artifact.get("stored_char_count") or 0)
    note = format_tool_output_artifact_recovery_note(
        artifact_id=artifact_id,
        chunk_count=chunk_count,
        stored_char_count=stored_char_count,
    )
    return note, saved_path


def format_tool_output_artifact_recovery_note(*, artifact_id: str, chunk_count: int, stored_char_count: int) -> str:
    """Return the provider-visible note for reading a raw-output artifact."""

    return (
        "[Raw output stored as artifact. "
        f"Artifact id: {artifact_id}. Chunks: {chunk_count}. "
        f"Stored output: {stored_char_count} chars. "
        "Read chunks with read_long_tool_output("
        f"'{artifact_id}', chunk_number) where chunk_number is 1-{chunk_count}"
        "; pass line_numbers=true for absolute source lines; use descendant_thread_id only when reading a descendant thread's artifact.]"
    )


def estimate_tool_output_artifact_recovery_note(
    full_output: str,
    *,
    original_char_count: Optional[int] = None,
    output_capped: bool = False,
) -> str:
    """Estimate the recovery note before allocating an artifact on disk."""

    if not isinstance(full_output, str):
        full_output = str(full_output or "")
    stored_output, capped, detected_original_char_count = _capped_tool_output(full_output)
    if original_char_count is None:
        original_char_count = detected_original_char_count
    else:
        try:
            original_char_count = max(int(original_char_count), detected_original_char_count)
        except Exception:
            original_char_count = detected_original_char_count
    _ = bool(output_capped or capped or original_char_count > len(stored_output))
    chunks = _split_tool_output_chunks(stored_output)
    return format_tool_output_artifact_recovery_note(
        artifact_id="x" * 8,
        chunk_count=len(chunks),
        stored_char_count=len(stored_output),
    )


def stash_tool_output_and_build_preview(
    db,
    thread_id: str,
    tool_call_id: str,
    full_output: str,
    *,
    max_lines: int = PREVIEW_MAX_LINES,
    max_chars: int = PREVIEW_MAX_CHARS,
    original_char_count: Optional[int] = None,
    output_capped: bool = False,
    publication_presentation: Any = None,
) -> tuple:
    """Persist tool output as a thread-owned artifact and return ``(preview, saved_path)``.

    Artifacts are stored under ``.egg/egg_outputs/<thread_id>/<artifact_id>``.
    The LLM-facing preview deliberately exposes only the short artifact id and
    ``read_long_tool_output(...)`` usage, not filesystem paths.
    """
    artifact = _stash_tool_output_artifact(
        thread_id,
        tool_call_id,
        full_output,
        original_char_count=original_char_count,
        output_capped=output_capped,
    )
    stored_output = str(artifact.get("stored_output") or "")
    saved_path = str(artifact.get("saved_path") or "")
    artifact_id = str(artifact.get("artifact_id") or "")
    chunk_count = int(artifact.get("chunk_count") or 1)
    stored_char_count = int(artifact.get("stored_char_count") or 0)
    original_char_count = int(artifact.get("original_char_count") or stored_char_count)
    stored_line_count = int(artifact.get("stored_line_count") or 0)
    original_line_count = int(artifact.get("original_line_count") or stored_line_count)
    capped = bool(artifact.get("capped"))

    preview_body = _tool_output_preview_text(stored_output, max_lines=max_lines, max_chars=max_chars)
    if preview_body != stored_output:
        preview_body = preview_body.rstrip()
    if publication_presentation:
        from .tool_output_presentation import apply_output_presentation

        preview_body = apply_output_presentation(preview_body, publication_presentation)

    if artifact_id:
        note = (
            f"[Preview only — first {min(stored_line_count, max_lines)} lines / "
            f"{min(stored_char_count, max_chars)} chars shown. "
            f"Artifact id: {artifact_id}. Chunks: {chunk_count}. "
        )
        if capped:
            note += (
                f"Stored output capped at {stored_char_count} of {original_char_count} chars. "
            )
        else:
            note += f"Stored output: {stored_char_count} chars. "
        note += (
            "Read chunks with read_long_tool_output("
            f"'{artifact_id}', chunk_number) where chunk_number is 1-{chunk_count}"
            "; pass line_numbers=true for absolute source lines; use descendant_thread_id only when reading a descendant thread's artifact.]"
        )
    else:
        note = (
            f"[Output truncated for preview ({original_line_count} lines, "
            f"{original_char_count} chars); artifact could not be saved to disk."
        )
        if capped:
            note += f" Output was capped at {stored_char_count} chars."
        note += "]"

    return f"{preview_body}\n\n{note}" if preview_body else note, saved_path


def _finalize_auto_tool_output(
    db,
    thread_id: str,
    tool_call_id: str,
    full_output: str,
    *,
    tool_name: str = "",
    tool_args: Any = None,
    finished_reason: str = "",
    origin: str = "runner",
    user_tool_call: bool = False,
    tool_metadata: Optional[Dict[str, Any]] = None,
    original_char_count: Optional[int] = None,
    output_capped: bool = False,
    publication_presentation: Any = None,
    expected_event_seq: int,
    writer: InvocationEventWriter,
):
    """Build policy output, then commit it through the TC4 authority."""

    from .output_policy import OutputPolicyRequest, create_output_policy_registry, decide_output_publication
    from .output_optimizer.config import get_thread_output_optimizer_policy_config

    if not isinstance(full_output, str):
        full_output = str(full_output or "")

    # Avoid policy/optimizer/artifact side effects when an explicit user
    # decision already won. This is only a fast path; finalize_tool_output still
    # performs the authoritative locked state/version check below.
    from .tool_state import _reduce_thread_events

    current = _reduce_thread_events(db, thread_id).tool_call_states.get(str(tool_call_id))
    if current is not None and current.output_decision is not None:
        return finalize_tool_output(
            db,
            thread_id,
            tool_call_id,
            decision=str(current.output_decision),
            source="automatic_policy",
            expected_event_seq=current.state_event_seq,
            invocation_writer=writer,
        )

    stored_char_count = len(full_output)
    stored_line_count = len(full_output.splitlines())
    original_count = original_char_count if original_char_count is not None else stored_char_count
    try:
        thread_output_optimizer_config = get_thread_output_optimizer_policy_config(db, thread_id)
        publication = decide_output_publication(
            create_output_policy_registry(),
            OutputPolicyRequest(
                db=db,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
                tool_name=str(tool_name or ""),
                tool_args=parse_tool_arguments(tool_args) if tool_args is not None else {},
                output=full_output,
                finished_reason=str(finished_reason or ""),
                origin=str(origin or "runner"),
                user_tool_call=bool(user_tool_call),
                tool_metadata=dict(tool_metadata or {}),
                thread_config=thread_output_optimizer_config,
                limits={
                    "long_output_line_threshold": LONG_OUTPUT_LINE_THRESHOLD,
                    "long_output_char_threshold": LONG_OUTPUT_CHAR_THRESHOLD,
                    "preview_max_lines": PREVIEW_MAX_LINES,
                    "preview_max_chars": PREVIEW_MAX_CHARS,
                },
                metadata={
                    "original_char_count": original_count,
                    "output_capped": output_capped,
                    "stored_char_count": stored_char_count,
                    "stored_line_count": stored_line_count,
                    "publication_presentation": dict(publication_presentation or {}),
                },
            ),
        )
    except Exception as exc:
        raise ToolOutputPlanError(
            thread_id,
            str(tool_call_id),
            f"Automatic output policy failed: {type(exc).__name__}: {exc}",
        ) from exc
    plan = ToolOutputPublicationPlan(
        decision=publication.decision,
        preview=publication.preview,
        reason=publication.reason,
        artifact_path=publication.artifact_path,
        channels=dict(publication.channels or {}),
        metadata={
            "line_count": stored_line_count,
            "char_count": stored_char_count,
            "original_char_count": original_count,
            "output_capped": bool(output_capped),
            "publication_presentation": dict(publication_presentation or {}),
        },
    )
    return finalize_tool_output(
        db,
        thread_id,
        tool_call_id,
        decision=publication.decision,
        source="automatic_policy",
        expected_event_seq=expected_event_seq,
        publication_plan=plan,
        invocation_writer=writer,
    )


def _tool_output_content_parts_for_transcript(tool_name: str, output: str) -> Optional[List[Dict[str, Any]]]:
    """Return canonical content parts for tool outputs that provide them.

    Tool outputs remain stored as plain text in ``tool_call.finished`` for audit
    and policy decisions.  Some tools, notably ``generate_image``, also return a
    small JSON envelope containing Egg content parts.  Publishing those parts in
    the final transcript lets UIs render artifact cards while provider context
    still receives plain text via ``content_to_plain_text``.
    """

    if str(tool_name or "") not in {
        "generate_image",
        "add_local_file_to_model_context",
        "add_provider_artifact_to_model_context",
        # Compatibility for already-persisted in-flight tool outputs from the
        # brief period when these LLM-facing tools used terminal-style names.
        "attach",
        "attach_output",
    } or not isinstance(output, str) or not output.strip():
        return None
    try:
        payload = json.loads(output)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    parts = payload.get("content_parts")
    if not isinstance(parts, list):
        return None
    try:
        return validate_content_parts(parts)
    except Exception:
        return None


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
    writer: Optional[InvocationEventWriter] = None,
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
    event_writer = writer or db.invocation_writer(thread_id, invoke_id)
    event_writer.append_event(
        event_id=os.urandom(10).hex(),
        type_='stream.delta',
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

    1. LLM/tool-call supplied ``timeout`` or legacy timeout aliases when they
       parse as a positive number.
    2. ``RunnerConfig.tool_timeout_sec`` when set to a positive number.
    3. Global default timeout when set to a positive number.

    ``None`` means no active timeout.  Invalid or non-positive LLM values are
    treated as absent and fall back to the next configured source; this matches
    the intent documented in the tool schemas and avoids misleading countdowns.
    """
    args = parse_tool_arguments(arguments)
    timeout = resolve_tool_timeout_arg(args)
    if timeout is not None:
        return timeout
    for candidate in (config_timeout_sec, default_timeout_sec):
        timeout = resolve_tool_timeout_arg({'timeout': candidate})
        if timeout is not None:
            return timeout
    return None


def emit_tool_summary_event(
    db,
    *,
    thread_id: str,
    invoke_id: Optional[str],
    tool_call_id: str,
    tool_name: str = "",
    summary: str,
    writer: Optional[InvocationEventWriter] = None,
) -> None:
    """Append a persisted tool_call.summary event for live status display."""
    if not isinstance(summary, str) or not summary:
        return
    if not invoke_id:
        raise ValueError("invoke_id is required for runner-owned tool summaries")
    event_writer = writer or db.invocation_writer(thread_id, invoke_id)
    event_writer.append_event(
        event_id=os.urandom(10).hex(),
        type_='tool_call.summary',
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
    writer: Optional[InvocationEventWriter] = None,
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
            writer=writer,
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
                writer=writer,
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
        writer=writer,
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
    # API/provider inactivity timeout: None = 600s default, 0 = no timeout,
    # >0 = timeout in seconds.  For streaming providers this is a pre-first-
    # response / inter-event inactivity timeout, not a total generation cap.
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
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None,
                 image_generation_models_path: Optional[str] = None):
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
        if image_generation_models_path is not None:
            self.image_generation_models_path = image_generation_models_path
        else:
            try:
                from eggllm.config import default_image_generation_models_path
                self.image_generation_models_path = str(default_image_generation_models_path(self.models_path))
            except Exception:
                self.image_generation_models_path = str(Path(self.models_path).with_name('image-generation-models.json'))
        self._invocation_writer: Optional[InvocationEventWriter] = None

    def _owned_writer(self, invoke_id: str) -> InvocationEventWriter:
        writer = self._invocation_writer
        if writer is not None and writer.invoke_id == invoke_id:
            return writer
        return self.db.invocation_writer(self.thread_id, invoke_id)

    def _owned_append(
        self,
        invoke_id: str,
        *,
        type_: str,
        payload: Dict[str, Any],
        msg_id: Optional[str] = None,
        chunk_seq: Optional[int] = None,
    ) -> int:
        return self._owned_writer(invoke_id).append_event(
            event_id=os.urandom(10).hex(),
            type_=type_,
            payload=payload,
            msg_id=msg_id,
            chunk_seq=chunk_seq,
        )

    def _has_compaction_summary_request_between(self, start_event_seq: int, before_event_seq: int) -> bool:
        rows = self.db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>=? AND event_seq<? ORDER BY event_seq ASC",
            (self.thread_id, int(start_event_seq), int(before_event_seq)),
        ).fetchall()
        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
            except Exception:
                continue
            if isinstance(payload, dict) and (payload.get('compaction_summary_request') or payload.get('auto_compaction_request')):
                return True
        return False

    async def _maybe_auto_continue_after_ra1_failure(self, ra: RunnerActionable) -> None:
        if ra.kind != 'RA1_llm':
            return
        try:
            from .api import append_auto_compaction_summary_request, append_recovery_notice, continue_thread, create_snapshot, get_thread_recovery
            from .runner_recovery import (
                check_recovery_fence,
                find_latest_recovery_source_after,
                format_auto_continue_notice,
                recovery_attempt_count,
            )
        except Exception:
            return

        try:
            if not get_thread_recovery(self.db, self.thread_id).auto_continue_on_error:
                return
        except Exception:
            return

        source = find_latest_recovery_source_after(self.db, self.thread_id, int(ra.triggering_event_seq))
        if source is None:
            return
        decision = source.decision
        trigger_msg_id = str(ra.msg_id or '') or None
        extra_base = {
            'auto_continue': True,
            'trigger_msg_id': trigger_msg_id,
            'source_msg_id': source.msg_id,
            'source_event_seq': source.event_seq,
            'decision_category': decision.category,
        }

        if not decision.retriable:
            if decision.category in {'context_length', 'max_output'}:
                try:
                    # A failure while answering a compaction checkpoint request
                    # should stop for the user instead of recursively queuing
                    # another checkpoint request.
                    if not self._has_compaction_summary_request_between(ra.triggering_event_seq, source.event_seq):
                        summary_result = append_auto_compaction_summary_request(
                            self.db,
                            self.thread_id,
                            selector=source.msg_id if decision.category == 'max_output' else (trigger_msg_id or 'last_message'),
                        )
                        if summary_result.success:
                            append_recovery_notice(
                                self.db,
                                self.thread_id,
                                format_auto_continue_notice(
                                    decision,
                                    action='compaction scheduled',
                                    trigger_msg_id=trigger_msg_id,
                                    source_msg_id=source.msg_id,
                                    detail=summary_result.message,
                                ),
                                extra={**extra_base, 'action': 'compaction_scheduled'},
                            )
                            create_snapshot(self.db, self.thread_id)
                            return
                except Exception:
                    pass
            append_recovery_notice(
                self.db,
                self.thread_id,
                format_auto_continue_notice(
                    decision,
                    action='stopped',
                    trigger_msg_id=trigger_msg_id,
                    source_msg_id=source.msg_id,
                ),
                extra={**extra_base, 'action': 'stopped'},
            )
            return

        if recovery_attempt_count(self.db, self.thread_id, trigger_msg_id) >= 1:
            append_recovery_notice(
                self.db,
                self.thread_id,
                format_auto_continue_notice(
                    decision,
                    action='stopped',
                    trigger_msg_id=trigger_msg_id,
                    source_msg_id=source.msg_id,
                    detail='automatic continue attempt cap reached',
                ),
                extra={**extra_base, 'action': 'stopped', 'stop_reason': 'attempt_cap'},
            )
            return

        delay_sec = float(decision.delay_sec or 0.0)
        append_recovery_notice(
            self.db,
            self.thread_id,
            format_auto_continue_notice(
                decision,
                action='scheduled',
                trigger_msg_id=trigger_msg_id,
                source_msg_id=source.msg_id,
            ),
            extra={**extra_base, 'action': 'scheduled', 'delay_sec': delay_sec},
        )
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

        fence = check_recovery_fence(
            self.db,
            self.thread_id,
            trigger_msg_id=trigger_msg_id,
            source_msg_id=source.msg_id,
            source_event_seq=source.event_seq,
            max_attempts=1,
        )
        if not fence.ok:
            append_recovery_notice(
                self.db,
                self.thread_id,
                format_auto_continue_notice(
                    decision,
                    action='stopped',
                    trigger_msg_id=trigger_msg_id,
                    source_msg_id=source.msg_id,
                    detail=fence.reason,
                ),
                extra={**extra_base, 'action': 'stopped', 'stop_reason': fence.reason},
            )
            return

        result = continue_thread(self.db, self.thread_id, msg_id=trigger_msg_id)
        if result.success:
            append_recovery_notice(
                self.db,
                self.thread_id,
                format_auto_continue_notice(
                    decision,
                    action='applied',
                    trigger_msg_id=trigger_msg_id,
                    source_msg_id=source.msg_id,
                    detail=result.message,
                ),
                extra={**extra_base, 'action': 'applied', 'delay_sec': delay_sec},
            )
            create_snapshot(self.db, self.thread_id)
        else:
            append_recovery_notice(
                self.db,
                self.thread_id,
                format_auto_continue_notice(
                    decision,
                    action='stopped',
                    trigger_msg_id=trigger_msg_id,
                    source_msg_id=source.msg_id,
                    detail=result.message,
                ),
                extra={**extra_base, 'action': 'stopped', 'stop_reason': result.message},
            )

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

        try:
            row_open = self.db.current_open(self.thread_id)
            if row_open:
                lease_until = row_open['lease_until']
                if lease_until and lease_until > _utcnow_iso():
                    return False
        except Exception:
            pass

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
        invocation_writer = self.db.invocation_writer(self.thread_id, invoke_id)
        self._invocation_writer = invocation_writer

        # Resolve current model for this turn from eggthreads API so that
        # the provider call and the event annotations stay in sync. Fall
        # back to the LLM client's current_model_key if needed.
        current_model: Optional[str] = None
        concrete_model_info: Optional[Dict[str, Any]] = None
        current_model_from_thread = False
        try:
            from .api import current_thread_model, current_thread_model_info
            current_model = current_thread_model(self.db, self.thread_id)
            concrete_model_info = current_thread_model_info(self.db, self.thread_id)
            current_model_from_thread = bool(current_model)
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
        deferred_model_selection_error: Optional[Exception] = None
        if ra.kind == 'RA1_llm' and current_model and current_model_from_thread:
            try:
                if concrete_model_info:
                    # Try set_model_with_config if available (eggllm >= 0.1.0)
                    if hasattr(self.llm, 'set_model_with_config'):
                        self.llm.set_model_with_config(current_model, concrete_model_info)
                    else:
                        self.llm.set_model(current_model)
                else:
                    self.llm.set_model(current_model)
            except Exception as e:
                # Do not continue with a mismatched llm.current_model_key while
                # recording events as current_model.  Surface the error through
                # the normal stream/error path after stream.open is persisted.
                deferred_model_selection_error = e

        # Open streaming event tagged with model_key and kind so that
        # downstream boundary detection can distinguish RA1 from
        # tool streaming.
        invocation_writer.append_event(
            event_id=os.urandom(10).hex(),
            type_='stream.open',
            msg_id=os.urandom(10).hex(),
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
            invocation_writer.append_event(
                event_id=os.urandom(10).hex(),
                type_='stream.delta',
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
        lease_lost = False
        context_length_error: Optional[str] = None
        ra1_emitted_assistant_tool_calls = False
        try:
            if ra.kind == 'RA1_llm':
                if deferred_model_selection_error is not None:
                    raise deferred_model_selection_error

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
                            self._owned_append(
                                invoke_id,
                                type_='msg.create',
                                msg_id=os.urandom(10).hex(),
                                payload={
                                    'role': 'system',
                                    'content': f'LLM/runner error: {error_msg}',
                                    'no_api': True,
                                    'runner_error': True,
                                },
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
                ra1_emitted_assistant_tool_calls = await self._run_ra1_llm(invoke_id, current_model, ra)

            elif ra.kind in ('RA2_tools_assistant', 'RA3_tools_user'):
                # ---------------- RA2/RA3: tool calls ----------------
                # For now we do not stream tool execution output separately
                # via additional LLM calls; we simply execute tools for
                # approved tool calls and advance their states.
                await self._run_ra_tools(invoke_id, current_model, ra)

        except LeaseLost:
            # Database fencing, not cooperative cancellation, is authoritative.
            was_cancelled = True
            lease_lost = True
            stop_flag = True
        except asyncio.CancelledError:
            # Cooperative shutdown/cancellation should not be recorded as an
            # LLM/runner error. Defer re-raising until after stream/lease cleanup.
            was_cancelled = True
        except (ToolOutputPlanError, ToolOutputPersistenceError, ToolOutputStateConflict) as e:
            # TC4 finalization failures are deliberately retriable. Do not append
            # a generic runner error (which would move the turn boundary or hide
            # the pending output prompt); leave the finished output in TC4.
            print(f"Tool output finalization pending retry: {e}")
        except Exception as e:
            if ra.kind == 'RA1_llm' and _is_context_length_exceeded_error(e):
                context_length_error = str(e) if str(e) else f"{type(e).__name__}: (no message)"
                try:
                    # Advance the failed RA1 boundary; the summary request is
                    # appended after stream.close so it is the next RA1 turn.
                    _append_delta({'reason': f'LLM/runner context length exceeded: {context_length_error}', 'model_key': current_model})
                except LeaseLost:
                    raise
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
                except LeaseLost:
                    raise
                except Exception:
                    pass
                try:
                    err_payload = {
                        'role': 'system',
                        'content': f'LLM/runner error: {error_msg}',
                        'no_api': True,
                        'runner_error': True,
                    }
                    if current_model:
                        err_payload['model_key'] = current_model
                    self._owned_append(
                        invoke_id,
                        type_='msg.create',
                        msg_id=os.urandom(10).hex(),
                        payload=err_payload,
                    )
                    if 'context length' in str(error_msg).lower() or 'context limit' in str(error_msg).lower():
                        context_length_error = error_msg
                    print(f"Runner error: {error_msg}")
                except LeaseLost:
                    raise
                except Exception:
                    pass
        finally:
            stop_flag = True
            try:
                hb_task.cancel()
                await asyncio.gather(hb_task, return_exceptions=True)
            except Exception:
                pass

        # Stream close is itself lease-fenced. A stale owner emits nothing.
        # Cooperative task cancellation is different from lease loss: if this
        # invocation still owns a live lease, close and release it before
        # propagating cancellation. Never release after a fenced write proves
        # that another owner took over (or that this lease expired).
        if not lease_lost:
            try:
                invocation_writer.close(event_id=os.urandom(10).hex())
            except LeaseLost:
                lease_lost = True
                was_cancelled = True

        if lease_lost:
            if self._invocation_writer is invocation_writer:
                self._invocation_writer = None
            raise asyncio.CancelledError

        if was_cancelled:
            try:
                invocation_writer.release()
            except LeaseLost:
                # An interrupt/takeover may race cooperative cancellation. The
                # exact-owner predicate guarantees we cannot delete its lease.
                pass
            finally:
                if self._invocation_writer is invocation_writer:
                    self._invocation_writer = None
            raise asyncio.CancelledError

        # Rebuild snapshot once for durability/token metadata. The recap is a
        # property of the just-completed invocation, so read only its final
        # message tail instead of walking a very large compatibility snapshot.
        try:
            from .api import create_snapshot_async

            await create_snapshot_async(self.db, self.thread_id)
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

                row = self.db.conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE thread_id=? AND invoke_id=? AND type='msg.create' "
                    "ORDER BY event_seq DESC LIMIT 1",
                    (self.thread_id, invoke_id),
                ).fetchone()
                try:
                    payload_json = row[0] if row is not None else None
                    payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
                except Exception:
                    payload = {}
                content = payload.get('content') if isinstance(payload, dict) and payload.get('role') == 'assistant' else None
                last_assist = content_to_plain_text(content) if isinstance(content, (str, list)) else None
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
                    err_payload = {
                        'role': 'system',
                        'content': f'LLM/runner error: {recovery_error}',
                        'no_api': True,
                        'runner_error': True,
                    }
                    if current_model:
                        err_payload['model_key'] = current_model
                    self._owned_append(
                        invoke_id,
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
            has_pending_assistant_tool = bool(ra1_emitted_assistant_tool_calls)

        recovery_compaction_queued = False
        if ra.kind == 'RA1_llm' and not was_cancelled and context_length_error is None and not has_pending_assistant_tool:
            try:
                from .api import _normal_user_messages_after_seq, auto_compact_summary_enabled, create_snapshot, maybe_auto_compact_thread, resolve_auto_compact_threshold
                from .runner_recovery import find_latest_recovery_source_after
                from .token_count import provider_context_token_stats

                threshold = resolve_auto_compact_threshold(
                    self.db,
                    self.thread_id,
                    self.cfg.auto_compact_threshold_tokens,
                    models_path=self.models_path,
                    all_models_path=self.all_models_path,
                )
                if threshold.enabled and threshold.threshold_tokens is not None:
                    stats = provider_context_token_stats(self.db, self.thread_id)
                    current_tokens = int(stats.get('context_tokens') or 0)
                    queued_users = _normal_user_messages_after_seq(
                        self.db,
                        self.thread_id,
                        int(ra.triggering_event_seq),
                    )
                    recovery_source = find_latest_recovery_source_after(
                        self.db,
                        self.thread_id,
                        int(ra.triggering_event_seq),
                    )
                    recovery_checkpoint_resume = bool(
                        recovery_source is not None
                        and (
                            recovery_source.decision.retriable
                            or recovery_source.decision.category == 'max_output'
                        )
                    )
                    checkpoint_resume = bool(queued_users) or recovery_checkpoint_resume
                    compaction_selector = (
                        str(queued_users[0].get('msg_id'))
                        if queued_users and queued_users[0].get('msg_id')
                        else recovery_source.msg_id if recovery_source is not None and recovery_source.decision.category == 'max_output'
                        else (str(ra.msg_id or '') or 'last_message') if recovery_checkpoint_resume else 'last_llm'
                    )
                    summary_mode_enabled = auto_compact_summary_enabled()
                    auto_result = maybe_auto_compact_thread(
                        self.db,
                        self.thread_id,
                        threshold_tokens=threshold.threshold_tokens,
                        context_tokens=current_tokens,
                        selector=compaction_selector,
                        checkpoint_resume=checkpoint_resume,
                        summary_mode=summary_mode_enabled,
                    )
                    if auto_result.triggered:
                        create_snapshot(self.db, self.thread_id)
                        recovery_compaction_queued = bool(summary_mode_enabled and recovery_checkpoint_resume)
            except Exception as e:
                # Token accounting/auto-compaction is best-effort; do not block
                # the existing runner path on advisory compaction.
                print(f"Warning: auto compaction check failed: {e}")

        # Release through the same exact-owner, unexpired lease authority.
        try:
            invocation_writer.release()
        except LeaseLost:
            raise asyncio.CancelledError
        finally:
            if self._invocation_writer is invocation_writer:
                self._invocation_writer = None

        if ra.kind == 'RA1_llm' and not was_cancelled and context_length_error is None and not recovery_compaction_queued:
            try:
                await self._maybe_auto_continue_after_ra1_failure(ra)
            except Exception as e:
                print(f"Warning: auto-continue recovery check failed: {e}")

        if was_cancelled:
            raise asyncio.CancelledError
        return True

    def _load_ra1_provider_projection(self):
        """Capture and load the one canonical message view for an RA1 turn."""

        from .projection import load_thread_projection

        watermark = self.db.max_event_seq(self.thread_id)
        return load_thread_projection(self.db, self.thread_id, watermark)

    async def _run_ra1_llm(
        self,
        invoke_id: str,
        current_model: Optional[str],
        ra: RunnerActionable,
    ) -> bool:
        """Handle RA1: perform a single LLM call, streaming deltas,
        and append the final assistant message with optional tool_calls.

        Returns True when the persisted assistant message declared tool_calls
        that should be handled by a follow-up RA2 turn.
        """
        from .api import filter_projection_for_compaction_provider_context

        # Capture one semantic input boundary after lease acquisition. The RA1
        # trigger answers why this invocation runs; the canonical projection
        # supplies every effective message visible at that exact boundary.
        projection = self._load_ra1_provider_projection()
        provider_context_watermark = projection.through_event_seq
        trigger = next(
            (
                message
                for message in projection.messages
                if message.msg_id == str(ra.msg_id or "")
                and message.created_event_seq == int(ra.triggering_event_seq)
            ),
            None,
        )
        if trigger is None:
            return False
        # Keep the discovery reducer authoritative for why RA1 was selected.
        # A newly appended edit after discovery may legitimately change the
        # projected payload, but must not make this already leased turn vanish;
        # deletion/continue does, because the trigger is then absent entirely.
        ev: Dict[str, Any] = {
            "event_seq": trigger.created_event_seq,
            "msg_id": None if trigger.msg_id.startswith("event:") else trigger.msg_id,
        }

        base_messages: List[Dict[str, Any]] = []
        thinking_policy: Optional[str] = None
        thinking_key: Optional[str] = None
        # Some providers (e.g. Gemini 3) require exact opaque thinking blobs.
        encrypted_thinking_mode: Optional[str] = None
        try:
            opts = self._model_thinking_options(current_model)
            tp = opts.get("thinking_content_policy")
            if isinstance(tp, str) and tp.strip():
                thinking_policy = tp.strip().lower()
            tk = opts.get("thinking_content_key")
            if isinstance(tk, str) and tk.strip():
                thinking_key = tk.strip()
        except Exception:
            thinking_policy = None
            thinking_key = None

        msgs = filter_projection_for_compaction_provider_context(
            self.db, projection
        )

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
                    if m.get('role') == 'user':
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
            tool_content_text = content_to_plain_text(content)
            # Compute optional thinking text according to policy
            thinking_text = _maybe_include_reasoning(m, idx)
            encrypted_thinking_val = _maybe_include_encrypted_thinking(m, idx)
            # Determine the outbound thinking key, defaulting
            # to the provider's native "reasoning_content" if
            # no explicit key was configured.
            out_thinking_key = thinking_key or 'reasoning_content'

            tcs = m.get('tool_calls') or []

            if r == 'assistant' and isinstance(tcs, list) and tcs:
                # Assistant messages with tool_calls may also
                # carry thinking. We forward tool_calls plus any
                # allowed thinking under the configured key.
                # NOTE: content field is required by some providers
                # (e.g., StepFun) even when empty.
                msg_out: Dict[str, Any] = {
                    'role': 'assistant',
                    'content': content,
                    'tool_calls': tcs,
                }
                if m.get('msg_id'):
                    msg_out['msg_id'] = m.get('msg_id')
                if m.get('event_seq') is not None:
                    msg_out['event_seq'] = m.get('event_seq')
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
                # Preserve structured attachment-producing tool
                # outputs until expand_tool_attachment_messages_for_provider()
                # can add a synthetic user-role visual/file input.
                # Other structured tool outputs (for example generated
                # provider artifact cards) should still reach providers
                # as plain text, because tool-role messages cannot carry
                # native multimodal blocks portably.
                tool_provider_content = (
                    content
                    if isinstance(content, list) and content_has_attachments(content, validate=False)
                    else tool_content_text
                )
                obj = {'role': 'tool', 'content': tool_provider_content}
                if m.get('msg_id'):
                    obj['msg_id'] = m.get('msg_id')
                if m.get('event_seq') is not None:
                    obj['event_seq'] = m.get('event_seq')
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
                if m.get('msg_id'):
                    msg_out['msg_id'] = m.get('msg_id')
                if m.get('event_seq') is not None:
                    msg_out['event_seq'] = m.get('event_seq')
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

        # Lower Egg-native attachment content parts at the provider boundary.
        # Current-turn unsupported attachments fail fast; older unsupported
        # attachments become explicit textual placeholders.
        try:
            from .attachment_lowering import AttachmentLoweringContext, expand_tool_attachment_messages_for_provider, lower_messages_for_provider

            model_cfg: Dict[str, Any] = {}
            try:
                if hasattr(self.llm, 'registry') and hasattr(self.llm, 'current_model_key'):
                    if hasattr(self.llm.registry, 'get_effective_model_config'):
                        model_cfg = self.llm.registry.get_effective_model_config(self.llm.current_model_key)
                    else:
                        model_cfg = self.llm.registry.get_model_config(self.llm.current_model_key)
            except Exception:
                model_cfg = {}
            api_type = str(model_cfg.get('api_type') or 'chat_completions') if isinstance(model_cfg, dict) else 'chat_completions'
            base_messages = expand_tool_attachment_messages_for_provider(base_messages)
            base_messages = lower_messages_for_provider(
                base_messages,
                AttachmentLoweringContext(
                    workspace=Path.cwd().resolve(),
                    db=self.db,
                    calling_thread_id=self.thread_id,
                    model_key=current_model,
                    model_config=model_cfg,
                    provider_api_type=api_type,
                ),
                current_msg_id=str(ev.get('msg_id') or '') or None,
            )
        except Exception:
            raise

        # Fail fast for any current-turn attachment that survived lowering
        # (for example because a future provider path bypasses a case). Older
        # attachments are already text placeholders.
        current_id = str(ev.get('msg_id') or '')
        for m in base_messages:
            if isinstance(m, dict) and current_id and m.get('msg_id') == current_id:
                if content_has_attachments(m.get('content'), validate=False):
                    raise ValueError("Current message contains attachments that could not be lowered for this model/provider.")

        # Final sanitation step before calling the provider: make sure that
        # user messages never carry "tool_calls" fields and that tool
        # exposure honours any per-thread tools configuration (e.g.
        # thread-wide tool disable, per-tool blacklists).
        tools_cfg = get_thread_tools_config(
            self.db,
            self.thread_id,
            through_event_seq=provider_context_watermark,
        )
        base_messages = self._sanitize_messages_for_api(
            base_messages,
            model_key=current_model,
            tools_cfg=tools_cfg,
        )

        # Apply per-thread tools configuration: this governs which tools
        # the LLM is allowed to see in this thread. User-initiated tools
        # (RA3) are still modelled as tool calls but are handled elsewhere
        # when executed.
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

        # Determine API/provider inactivity timeout: per-thread setting >
        # config setting > default (600s).  eggllm adapters enforce this as
        # connection/read inactivity rather than total streaming wall-clock.
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

        self._owned_append(
            invoke_id,
            type_='provider_request.started',
            payload={
                'timeout': api_timeout_int,
                'timeout_kind': 'inactivity',
                'model_key': current_model,
            },
        )

        interrupted = False
        lease_lost_during_stream = False
        transport_error_after_output: Optional[BaseException] = None

        def _persist_assistant_message(final: Dict[str, Any]) -> bool:
            """Persist a completed assistant turn and return whether it has tools.

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
            # Local usage metadata (api_usage/provider_usage) is intentionally
            # persisted for audit/cost accounting, then stripped by
            # _sanitize_messages_for_api before future provider requests.
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
                and not assistant_msg.get('reasoning_content')
                and not assistant_msg.get('incomplete')
                and not assistant_msg.get('incomplete_reason')
                and not assistant_msg.get('incomplete_details')):
                err_payload: Dict[str, Any] = {
                    'role': 'system',
                    'content': 'LLM error: empty assistant message returned by provider',
                    'no_api': True,
                    'runner_error': True,
                }
                if current_model:
                    err_payload['model_key'] = current_model
                self._owned_append(
                    invoke_id,
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
            self._owned_append(
                invoke_id,
                type_='msg.create',
                msg_id=os.urandom(10).hex(),
                payload=assistant_msg,
            )
            persisted_tool_calls = assistant_msg.get('tool_calls')
            return bool(isinstance(persisted_tool_calls, list) and persisted_tool_calls)

        try:
            extra_body: Optional[Dict[str, Any]] = None
            try:
                if hasattr(self.llm, 'registry') and hasattr(self.llm, 'current_model_key'):
                    params = self.llm.registry.merge_parameters(self.llm.current_model_key)
                    cache_key_name = params.get("prompt_cache_key") if isinstance(params, dict) else None
                    if cache_key_name:
                        extra_body = {cache_key_name: self.thread_id[-4:]}
            except Exception:
                pass
            async for raw in self.llm.astream_chat(
                base_messages,
                tools=tools_spec_to_use,
                tool_choice=tool_choice,
                timeout=api_timeout_int,
                extra_body=extra_body,
            ):
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
                                lease_lost_during_stream = True
                                break
                            chunk_seq += 1
                            self._owned_append(
                                invoke_id,
                                type_='stream.delta',
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
                                lease_lost_during_stream = True
                                break
                            chunk_seq += 1
                            self._owned_append(
                                invoke_id,
                                type_='stream.delta',
                                chunk_seq=chunk_seq,
                                payload=(
                                    {'reasoning_summary': reason, 'model_key': current_model}
                                    if is_reasoning_summary
                                    else {'reason': reason, 'model_key': current_model}
                                ),
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
                                lease_lost_during_stream = True
                                break
                            chunk_seq += 1
                            self._owned_append(
                                invoke_id,
                                type_='stream.delta',
                                chunk_seq=chunk_seq,
                                payload={
                                    'tool_call': {
                                        'id': tcid,
                                        'name': tool_calls_names_so_far.get(tcid, str(name or '')),
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
                        return _persist_assistant_message(final)
                if interrupted:
                    break
        except LeaseLost:
            interrupted = True
            lease_lost_during_stream = True
            raise
        except Exception as e:
            if assistant_text_parts or reasoning_parts or tool_calls_args_so_far:
                transport_error_after_output = e
            else:
                raise
        finally:
            # Provider interruption while the lease is still live may preserve
            # partial output. Lease loss itself must never append stale content.
            if interrupted and not lease_lost_during_stream and (assistant_text_parts or reasoning_parts):
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
                self._owned_append(
                    invoke_id,
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
            self._owned_append(
                invoke_id,
                type_='msg.create',
                msg_id=os.urandom(10).hex(),
                payload=partial_msg,
            )
            partial_tool_calls_value = partial_msg.get('tool_calls')
            return bool(isinstance(partial_tool_calls_value, list) and partial_tool_calls_value)

        return False


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


    def _sanitize_messages_for_api(
        self,
        messages: List[Dict[str, Any]],
        model_key: Optional[str] = None,
        *,
        tools_cfg: Any = None,
    ) -> List[Dict[str, Any]]:
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
            effective_tools_cfg = tools_cfg or get_thread_tools_config(
                self.db, self.thread_id
            )
            allow_raw = bool(
                getattr(effective_tools_cfg, 'allow_raw_tool_output', False)
            )
        except Exception:
            allow_raw = False

        # Get tool_call_id normalization strategy for this provider (e.g., "mistral9")
        normalize_strategy = self._get_tool_call_id_normalization_strategy(model_key)

        out: List[Dict[str, Any]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            m2 = dict(m)
            m2.pop("api_usage", None)
            m2.pop("provider_usage", None)
            # Local/UI-only optimizer observability metadata must not be sent
            # to providers.  The provider sees the already-approved preview in
            # content, while raw/audit metadata remains in events/snapshots.
            m2.pop("output_optimizer", None)
            if m2.get("answer_user_preserve_turn"):
                continue
            role = m2.get("role")

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

            content_value = m2.get("content")
            if isinstance(content_value, list) and (
                content_has_attachments(content_value, validate=False)
                or content_has_artifacts(content_value, validate=False)
            ):
                m2["content"] = content_to_plain_text(content_value)

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

        # Preserve-turn get-user calls may be separately declared while their
        # exact results are durably published later at the event-log tail. Fold
        # those lifecycle messages into one synthetic contiguous assistant/tool
        # block before the generic protocol safety net. This is provider-only
        # projection: the inspectable canonical transcript remains unchanged.
        out = self._coalesce_get_user_tool_protocol(out)

        # As a final safety net, enforce the OpenAI tools protocol
        # invariant that every assistant message with ``tool_calls`` is
        # immediately followed by tool-role messages responding to each
        # ``tool_call_id``.  If history ever violated this (for example
        # due to buggy older versions of the UI), we drop the offending
        # assistant/tool messages from the provider view so that new
        # turns can proceed instead of failing with a persistent
        # "tool_calls must be followed by tool messages" error.
        return self._enforce_assistant_toolcall_protocol(out)


    def _coalesce_get_user_tool_protocol(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Place unique completed get-user results beside their declarations.

        Canonical event order can contain several assistant get-user
        declarations followed later by their exact results. Provider protocols
        require each declaration to be immediately followed by its result(s).
        This provider-only projection iterates canonical messages in place,
        emits each unambiguous get-user-only declaration at its original
        position, emits its unique exact results immediately afterward, and
        skips only those result rows at their old positions.

        Identity accounting is restricted to get-user call IDs, so unrelated
        valid tool turns cannot disable repair. Reused IDs across declarations,
        duplicate IDs within a declaration, and duplicate exact-ID results are
        removed fail-closed rather than choosing or copying an ambiguous result.
        Mixed and incomplete declarations stay in their original shape for the
        generic protocol guard. Original assistant/tool-call objects are kept,
        preserving opaque top-level and per-call provider metadata.
        """

        get_user_name = "get_user_message_while_preserving_llm_turn"
        declaration_occurrences: dict[str, list[int]] = {}
        get_user_ids_by_declaration: dict[int, list[str]] = {}
        repairable_declarations: dict[int, tuple[Dict[str, Any], list[str]]] = {}

        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue
            get_user_ids: list[str] = []
            get_user_only = True
            for call in calls:
                if not isinstance(call, dict):
                    get_user_only = False
                    continue
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                call_id = call.get("id")
                if name == get_user_name:
                    if isinstance(call_id, str) and call_id:
                        get_user_ids.append(call_id)
                        declaration_occurrences.setdefault(call_id, []).append(index)
                    else:
                        get_user_only = False
                else:
                    get_user_only = False
            if get_user_ids:
                get_user_ids_by_declaration[index] = get_user_ids
            if get_user_only and len(get_user_ids) == len(calls):
                repairable_declarations[index] = (message, get_user_ids)

        participating_ids = set(declaration_occurrences)
        if not participating_ids:
            return messages

        results_by_id: dict[str, list[tuple[int, Dict[str, Any]]]] = {
            call_id: [] for call_id in participating_ids
        }
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in participating_ids:
                results_by_id[call_id].append((index, message))

        ambiguous_ids = {
            call_id
            for call_id in participating_ids
            if len(declaration_occurrences.get(call_id, [])) != 1
            or len(results_by_id.get(call_id, [])) > 1
        }
        ambiguous_declaration_indices = {
            index
            for index, call_ids in get_user_ids_by_declaration.items()
            if any(call_id in ambiguous_ids for call_id in call_ids)
        }
        ambiguous_result_indices = {
            result_index
            for call_id in ambiguous_ids
            for result_index, _result in results_by_id.get(call_id, [])
        }

        valid_groups: dict[int, list[tuple[int, Dict[str, Any]]]] = {}
        used_result_indices: set[int] = set()
        for index, (_message, call_ids) in repairable_declarations.items():
            if index in ambiguous_declaration_indices or len(set(call_ids)) != len(call_ids):
                continue
            if any(len(results_by_id.get(call_id, [])) != 1 for call_id in call_ids):
                # Incomplete declarations remain for the generic fail-closed
                # guard; do not partly repair a multi-call declaration.
                continue
            group_results = [results_by_id[call_id][0] for call_id in call_ids]
            if any(result_index <= index for result_index, _result in group_results):
                continue
            valid_groups[index] = group_results
            used_result_indices.update(result_index for result_index, _result in group_results)

        rebuilt: List[Dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index in ambiguous_declaration_indices or index in ambiguous_result_indices:
                continue
            if index in used_result_indices:
                continue
            rebuilt.append(message)
            if index in valid_groups:
                rebuilt.extend(result for _result_index, result in valid_groups[index])
        return rebuilt


    def _enforce_assistant_toolcall_protocol(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep only complete one-to-one assistant/tool protocol blocks.

        Every assistant ``tool_calls`` declaration must contain unique,
        non-empty IDs and be followed immediately by exactly one tool result for
        each ID. Malformed declarations and all orphan/duplicate tool rows are
        removed from provider context; canonical local history is untouched.
        """

        if not messages:
            return messages

        n = len(messages)
        valid_blocks: dict[int, list[int]] = {}
        claimed_tool_indices: set[int] = set()

        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                continue

            expected_ids: list[str] = []
            valid_declaration = True
            for call in calls:
                if not isinstance(call, dict):
                    valid_declaration = False
                    break
                call_id = call.get("id")
                if not call_id and isinstance(call.get("function"), dict):
                    call_id = call["function"].get("id")
                if not isinstance(call_id, str) or not call_id:
                    valid_declaration = False
                    break
                expected_ids.append(call_id)
            if not valid_declaration or len(set(expected_ids)) != len(expected_ids):
                continue

            expected = set(expected_ids)
            seen: set[str] = set()
            tool_indices: list[int] = []
            cursor = index + 1
            valid_results = True
            while cursor < n:
                candidate = messages[cursor]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                result_id = candidate.get("tool_call_id")
                if (
                    not isinstance(result_id, str)
                    or not result_id
                    or result_id not in expected
                    or result_id in seen
                ):
                    valid_results = False
                    break
                seen.add(result_id)
                tool_indices.append(cursor)
                cursor += 1

            if valid_results and seen == expected and len(tool_indices) == len(expected_ids):
                valid_blocks[index] = tool_indices
                claimed_tool_indices.update(tool_indices)

        out: List[Dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index in claimed_tool_indices:
                continue
            if not isinstance(message, dict):
                out.append(message)
                continue
            role = message.get("role")
            calls = message.get("tool_calls")
            if role == "assistant" and isinstance(calls, list) and calls:
                if index in valid_blocks:
                    out.append(message)
                    out.extend(messages[tool_index] for tool_index in valid_blocks[index])
                continue
            if role == "tool":
                continue
            out.append(message)
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
            try:
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
                    writer=self._owned_writer(invoke_id),
                )
            except LeaseLost:
                return False

        def _emit_summary(summary: str) -> None:
            try:
                emit_tool_summary_event(
                    self.db,
                    thread_id=self.thread_id,
                    invoke_id=invoke_id,
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.name or '',
                    summary=summary,
                    writer=self._owned_writer(invoke_id),
                )
            except LeaseLost:
                return

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
        if tools_cfg.policy_error:
            # The config already carries a durable/best-effort diagnostic. Keep
            # processing states so calls can terminate inspectably, but execute
            # no tool while policy authorization is unknown.
            policy_error_message = f"Tool policy unavailable; execution denied: {tools_cfg.policy_error}"
        else:
            policy_error_message = ""
        for tc in tool_calls:
            # Denied -> publish denial message and move to TC6
            if tc.state == 'TC2.2' and not tc.published:
                reason = 'Tool call execution denied.'
                msg = {
                    'role': 'tool',
                    'content': f"Tool call execution denied! Reason: {reason}",
                    'tool_call_id': tc.tool_call_id,
                    'name': tc.name,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                if current_model:
                    msg['model_key'] = current_model
                self._owned_append(
                    invoke_id,
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
                    disabled_msg = policy_error_message or (
                        f"Tool '{tc.name}' is not allowed for this thread and "
                        "was not executed."
                    )
                    finished_seq = self._owned_append(
                        invoke_id,
                        type_='tool_call.finished',
                        payload={
                            'tool_call_id': tc.tool_call_id,
                            'reason': 'policy_error' if policy_error_message else 'disabled',
                            'output': disabled_msg,
                        },
                    )
                    # Synthetic policy/disabled output goes through the same
                    # transactional TC4 authority as normal tool output.
                    synthetic_reason = (
                        'Auto: tool policy read failed closed'
                        if policy_error_message
                        else 'Auto: tool not allowed for this thread'
                    )
                    finalize_tool_output(
                        self.db,
                        self.thread_id,
                        tc.tool_call_id,
                        decision='whole',
                        source='automatic_synthetic',
                        reason=synthetic_reason,
                        expected_event_seq=finished_seq,
                        publication_plan=ToolOutputPublicationPlan(
                            decision='whole',
                            preview=disabled_msg,
                            reason=synthetic_reason,
                        ),
                        invocation_writer=self._owned_writer(invoke_id),
                    )
                    continue

                # ToolRegistry supports both sync and async tool
                # implementations. Synchronous tools run in a worker thread;
                # async tools are awaited directly.
                # Shared timeout resolution: LLM-specified > config > global default.
                tool_timeout_sec = resolve_tool_timeout_sec(
                    tc.arguments,
                    self.cfg.tool_timeout_sec if self.cfg is not None else None,
                    _default_tool_timeout_sec,
                )
                started_payload: Dict[str, Any] = {
                    'tool_call_id': tc.tool_call_id,
                    # A live UI may attach after the LLM tool-call argument
                    # deltas have streamed and after the RA1 stream has
                    # closed. Repeat the declaration metadata on execution
                    # start so a running tool remains inspectable without
                    # waiting for a final transcript redraw.
                    'name': tc.name,
                    'arguments': tc.arguments,
                }
                if tool_timeout_sec is not None:
                    started_payload['timeout'] = tool_timeout_sec
                self._owned_append(
                    invoke_id,
                    type_='tool_call.execution_started',
                    msg_id=None,
                    payload=started_payload,
                )

                # Create a cancel check that returns True if lease is lost (e.g., Ctrl+C)
                def make_cancel_check(db_path, thread_id, invoke_id):
                    # Thread-local storage for executor thread's own connection
                    import threading
                    local = threading.local()

                    def check():
                        try:
                            # Create a fresh connection per calling thread.
                            # Async tools poll on the event-loop thread while sync
                            # tools poll in an executor thread, and sqlite
                            # connections cannot cross that boundary.
                            import sqlite3
                            current_thread_id = threading.get_ident()
                            if (
                                not hasattr(local, 'conn')
                                or local.conn is None
                                or getattr(local, 'thread_id', None) != current_thread_id
                            ):
                                local.conn = sqlite3.connect(str(db_path), timeout=0.1)
                                local.thread_id = current_thread_id
                            row = local.conn.execute(
                                """
                                SELECT 1 FROM open_streams
                                WHERE thread_id=? AND invoke_id=? AND lease_until>datetime('now')
                                """,
                                (thread_id, invoke_id),
                            ).fetchone()
                            return row is None  # True = cancelled (lease lost)
                        except sqlite3.Error:
                            # Busy/transient DB errors do not establish lease loss.
                            return False
                    return check

                cancel_check = make_cancel_check(self.db.path, self.thread_id, invoke_id)
                stream_ctx = self._tool_stream_context(tc=tc, invoke_id=invoke_id, current_model=current_model)

                try:
                    tool_result = await self.tools.execute_async(
                        tc.name,
                        tc.arguments,
                        thread_id=self.thread_id,
                        invoke_id=invoke_id,
                        tool_call_id=tc.tool_call_id,
                        origin='runner',
                        initial_model_key=current_model,
                        tool_timeout_sec=tool_timeout_sec,
                        cancel_check=cancel_check,
                        db=self.db,
                        models_path=self.models_path,
                        all_models_path=self.all_models_path,
                        image_generation_models_path=self.image_generation_models_path,
                        stream=stream_ctx,
                        preserve_tool_result=True,
                    )
                    if isinstance(tool_result, ToolExecutionResult):
                        full_result = tool_result.output
                        finish_reason = tool_result.reason or 'success'
                        already_streamed = tool_result.streamed
                        publication_presentation = dict(tool_result.publication_presentation or {})
                    else:
                        full_result = tool_result
                        finish_reason = 'success'
                        already_streamed = False
                        publication_presentation = {}
                except asyncio.CancelledError:
                    full_result = (
                        "--- INTERRUPTED ---\n"
                        "Tool execution was interrupted because the runner task was cancelled."
                    )
                    try:
                        finished_seq = self._owned_append(
                            invoke_id,
                            type_='tool_call.finished',
                            payload={
                                'tool_call_id': tc.tool_call_id,
                                'reason': 'interrupted',
                                'output': full_result,
                            },
                        )
                        finalize_tool_output(
                            self.db,
                            self.thread_id,
                            tc.tool_call_id,
                            decision='whole',
                            source='automatic_synthetic',
                            reason='Runner task cancelled',
                            expected_event_seq=finished_seq,
                            publication_plan=ToolOutputPublicationPlan(
                                decision='whole',
                                preview=full_result,
                                reason='Runner task cancelled',
                            ),
                            invocation_writer=self._owned_writer(invoke_id),
                        )
                    except Exception:
                        pass
                    raise
                except Exception as e:
                    full_result = f"ERROR: {e}"
                    finish_reason = 'error'
                    already_streamed = False
                    publication_presentation = {}
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
                output_was_capped = False
                original_output_char_count = len(full_result)
                try:
                    full_result, output_was_capped, original_output_char_count = _capped_tool_output(full_result)
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
                        # approval/artifact path below.
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
                            writer=self._owned_writer(invoke_id),
                        )
                        if not ok:
                            cancelled = True
                            break
                        await asyncio.sleep(0)
                if cancelled and finish_reason == 'success':
                    finish_reason = 'interrupted'
                finished_seq = self._owned_append(
                    invoke_id,
                    type_='tool_call.finished',
                    payload={
                        'tool_call_id': tc.tool_call_id,
                        'reason': finish_reason,
                        'output': full_result,
                        'publication_presentation': publication_presentation,
                    },
                )
                # Auto output-approval: small outputs get decision='whole'
                # and go through verbatim; long outputs are stored as
                # artifacts and get decision='partial' with a preview that
                # references read_long_tool_output usage. A UI cancellation (Ctrl+C)
                # that already recorded an explicit decision is respected.
                parent_no_api = self._parent_msg_has_no_api(tc.parent_msg_id) if ra.kind == 'RA3_tools_user' else False
                _finalize_auto_tool_output(
                    self.db,
                    self.thread_id,
                    tc.tool_call_id,
                    full_result,
                    tool_name=tc.name,
                    tool_args=tc.arguments,
                    finished_reason=finish_reason,
                    origin=ra.kind,
                    user_tool_call=bool(ra.kind == 'RA3_tools_user'),
                    tool_metadata={
                        'ra_kind': ra.kind,
                        'parent_msg_id': tc.parent_msg_id,
                        'parent_role': tc.parent_role,
                        'parent_no_api': parent_no_api,
                        'tool_index': tc.index,
                    },
                    original_char_count=original_output_char_count,
                    output_capped=output_was_capped,
                    publication_presentation=publication_presentation,
                    expected_event_seq=finished_seq,
                    writer=self._owned_writer(invoke_id),
                )

            # Output approval done (TC5) -> publish final tool message based on
            # the last tool_call.output_approval payload.
            if tc.state == 'TC5':
                payload = tc.last_output_approval_payload or {}
                decision = payload.get('decision')
                preview = payload.get('preview') or ''
                finished_output = tc.finished_output or ''
                finished_reason = (tc.finished_reason or '').lower()
                from .tool_output_contract import requires_legacy_long_output_routing

                long_finished_output = bool(
                    requires_legacy_long_output_routing(tc.name)
                    and (
                        len(finished_output) > LONG_OUTPUT_CHAR_THRESHOLD
                        or _line_count(finished_output) > LONG_OUTPUT_LINE_THRESHOLD
                    )
                )
                has_artifact_recovery = bool(
                    _artifact_is_ready(str(payload.get('artifact_path') or ''))
                    and 'read_long_tool_output(' in str(preview)
                )
                legacy_long_content: Optional[str] = None
                if (
                    long_finished_output
                    and decision in ('whole', 'partial')
                    and not has_artifact_recovery
                ):
                    # Imported/pre-authority events may contain a long whole or
                    # partial preview without a recoverable artifact. Route it
                    # at the last publication boundary as a compatibility
                    # safety net. New decisions are handled transactionally by
                    # finalize_tool_output() before reaching TC5.
                    try:
                        legacy_long_content, _saved = stash_tool_output_and_build_preview(
                            self.db,
                            self.thread_id,
                            str(tc.tool_call_id),
                            finished_output,
                        )
                    except Exception:
                        legacy_long_content = finished_output[:PREVIEW_MAX_CHARS]
                        if len(finished_output) > PREVIEW_MAX_CHARS:
                            legacy_long_content = (
                                legacy_long_content.rstrip()
                                + "\n\n...[output truncated for preview]..."
                            )

                # Determine base content. Interrupted output decisions now go
                # through the same canonical artifact plan as every other
                # output. Keep a bounded compatibility fallback for legacy
                # whole/empty-preview events that predate that invariant.
                if finished_reason == 'interrupted':
                    if legacy_long_content is not None:
                        content = legacy_long_content
                    elif decision == 'partial' and preview:
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
                    elif legacy_long_content is not None:
                        content = legacy_long_content
                    elif decision == 'whole':
                        # Structured artifact content is safe only for a whole
                        # decision. A partial long-output decision must retain
                        # its bounded preview/read-tool recovery note.
                        structured_content = _tool_output_content_parts_for_transcript(tc.name, finished_output)
                        content = structured_content if structured_content is not None else str(preview)
                    else:
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
                no_api_flag = bool(ra.kind == 'RA3_tools_user' and (decision == 'omit' or parent_no_api))

                msg = {
                    'role': 'tool',
                    'content': content,
                    'tool_call_id': tc.tool_call_id,
                    'name': tc.name,
                    'user_tool_call': bool(ra.kind == 'RA3_tools_user'),
                }
                try:
                    from .output_optimizer.observability import optimizer_public_metadata_from_output_approval

                    output_optimizer_metadata = optimizer_public_metadata_from_output_approval(payload)
                    if output_optimizer_metadata:
                        msg['output_optimizer'] = output_optimizer_metadata
                except Exception:
                    pass
                # For user-initiated commands (RA3), keep the user turn
                # after publishing the tool result. The model should not
                # be invoked automatically; instead, the result becomes
                # part of the context for the *next* user message.
                if ra.kind == 'RA3_tools_user':
                    msg['keep_user_turn'] = True
                if no_api_flag:
                    msg['no_api'] = True
                if current_model:
                    msg['model_key'] = current_model
                self._owned_append(
                    invoke_id,
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
        return content_to_plain_text(content) if isinstance(content, (str, list)) else None

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

    # Not idle if waiting for API response (has an active open stream).  This
    # cheap lease check must run before actionability discovery so sticky
    # scheduling does not full-reduce large threads that are already leased.
    now_iso = _utcnow_iso()
    row = db.conn.execute(
        "SELECT 1 FROM open_streams WHERE thread_id = ? AND lease_until > ? LIMIT 1",
        (thread_id, now_iso),
    ).fetchone()
    if row:
        return False

    # Not idle if runnable
    if is_thread_runnable(db, thread_id):
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


def _active_open_thread_leases_bulk(db: ThreadsDB, thread_ids: List[str]) -> Dict[str, str]:
    """Return active lease expiry by thread id."""
    out: Dict[str, str] = {}
    now_iso = _utcnow_iso()
    for batch in _thread_id_batches(thread_ids):
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        try:
            cur = db.conn.execute(
                f"SELECT thread_id, lease_until FROM open_streams WHERE lease_until > ? AND thread_id IN ({placeholders})",
                (now_iso, *batch),
            )
            for row in cur.fetchall():
                out[str(row[0])] = str(row[1])
        except Exception:
            continue
    return out


def _active_open_threads_bulk(db: ThreadsDB, thread_ids: List[str]) -> Set[str]:
    """Return thread ids with currently active leases."""
    return set(_active_open_thread_leases_bulk(db, thread_ids))


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
                 models_path: Optional[str] = None, all_models_path: Optional[str] = None, tools: Optional[ToolRegistry] = None,
                 image_generation_models_path: Optional[str] = None):
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
        if image_generation_models_path is not None:
            self.image_generation_models_path = image_generation_models_path
        else:
            try:
                from eggllm.config import default_image_generation_models_path
                self.image_generation_models_path = str(default_image_generation_models_path(self.models_path))
            except Exception:
                self.image_generation_models_path = str(Path(self.models_path).with_name('image-generation-models.json'))
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
        last_active_lease: Dict[str, str] = {}
        previous_subtree_threads: Set[str] = set()

        # Sticky scheduling state
        last_run_end: Dict[str, float] = {}  # thread_id -> monotonic time when last run ended
        reserved_slots: Set[str] = set()     # threads with reserved slots (recently ran, within threshold)

        scheduler_work_since_yield = 0
        last_scheduler_yield_at = time.monotonic()
        last_session_reap_at = 0.0

        async def checkpoint_scheduler_fairness(*, force: bool = False) -> None:
            """Yield so scheduler bookkeeping cannot monopolize the TUI loop."""

            nonlocal scheduler_work_since_yield, last_scheduler_yield_at
            scheduler_work_since_yield += 1
            now = time.monotonic()
            if (
                not force
                and scheduler_work_since_yield < _SCHEDULER_FAIRNESS_CHECKS_PER_YIELD
                and (now - last_scheduler_yield_at) < _SCHEDULER_FAIRNESS_TIME_SLICE_SEC
            ):
                return

            scheduler_work_since_yield = 0
            last_scheduler_yield_at = now
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                await self.shutdown()
                raise

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
                    image_generation_models_path=self.image_generation_models_path,
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
            now = time.monotonic()
            if (now - last_session_reap_at) >= 30.0:
                try:
                    from .session import start_idle_auto_docker_reaper

                    start_idle_auto_docker_reaper(self.db)
                except Exception:
                    # Resource maintenance must not stop scheduler progress.
                    pass
                last_session_reap_at = now
            all_threads = self._collect_subtree(self.root)
            known_threads = set(all_threads)
            stale_scheduler_threads = (
                previous_subtree_threads
                | set(last_checked_seq)
                | set(last_active_lease)
                | set(last_run_end)
                | set(reserved_slots)
            ) - known_threads
            if stale_scheduler_threads:
                for tid in stale_scheduler_threads:
                    last_checked_seq.pop(tid, None)
                    last_active_lease.pop(tid, None)
                    last_run_end.pop(tid, None)
                reserved_slots -= stale_scheduler_threads
                _prune_reducer_cache_for_threads(str(self.db.path), stale_scheduler_threads)
            previous_subtree_threads = known_threads
            await checkpoint_scheduler_fairness(force=True)
            max_event_seqs = _max_event_seqs_bulk(self.db, all_threads)
            await checkpoint_scheduler_fairness(force=True)
            active_open_leases = _active_open_thread_leases_bulk(self.db, all_threads)
            active_open_threads = set(active_open_leases)
            await checkpoint_scheduler_fairness(force=True)
            scheduling_settings = _thread_scheduling_bulk(self.db, all_threads)
            await checkpoint_scheduler_fairness(force=True)

            for tid in list(last_active_lease):
                if tid not in active_open_threads:
                    last_active_lease.pop(tid, None)
                    last_checked_seq.pop(tid, None)

            # Update idle tracking and expire reservations (sticky scheduling)
            if self.cfg.sticky_scheduling:
                now = time.monotonic()

                # For each reserved thread, check if it's truly idle
                for tid in list(reserved_slots):
                    if tid in running_threads:
                        # Running threads are not idle - clear any idle timer
                        last_run_end.pop(tid, None)
                        await checkpoint_scheduler_fairness()
                        continue

                    if tid in active_open_threads:
                        # Active leased threads are not idle.  Use the bulk
                        # lease snapshot so sticky scheduling does not need an
                        # actionability check for threads already running.
                        last_run_end.pop(tid, None)
                        await checkpoint_scheduler_fairness()
                        continue

                    if not _is_thread_idle(self.db, tid):
                        # Thread is waiting for API/tool approval - not idle, clear timer
                        last_run_end.pop(tid, None)
                        await checkpoint_scheduler_fairness()
                        continue

                    # Thread is truly idle - start or continue idle timer
                    if tid not in last_run_end:
                        last_run_end[tid] = now  # Start idle timer
                    await checkpoint_scheduler_fairness()

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
                    await checkpoint_scheduler_fairness()
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
                    await checkpoint_scheduler_fairness()
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

                # Skip active leased threads before actionability discovery.
                # Keep a lease-specific watermark so an unchanged active lease
                # does not keep reaching this branch, but clear that watermark
                # above as soon as the lease disappears or expires so takeover
                # and post-lease work are still discovered.
                if tid in active_open_threads:
                    last_checked_seq[tid] = max_seq
                    last_active_lease[tid] = active_open_leases[tid]
                    await checkpoint_scheduler_fairness()
                    continue

                if max_seq == last_checked_seq.get(tid, -1):
                    await checkpoint_scheduler_fairness()
                    continue
                checked_seqs[tid] = max_seq

                ra = discover_runner_actionable_cached(self.db, tid)
                if ra is not None:
                    runnable_candidates.append((tid, ra, runner_actionable_resource_class(ra)))
                else:
                    # Thread is NOT runnable - mark as checked so we skip it
                    # until its events change
                    last_checked_seq[tid] = checked_seqs[tid]
                await checkpoint_scheduler_fairness()

            # Second pass: schedule runnable threads.  Only RA1/LLM work
            # consumes available_llm_slots; RA2/RA3 tool work is scheduled
            # independently (unless an explicit tool cap is configured).
            # (candidates are already in priority order from all_threads)
            for tid, _ra, resource_class in runnable_candidates:
                if resource_class == "llm" and available_llm_slots <= 0:
                    await checkpoint_scheduler_fairness()
                    continue
                if resource_class == "tool" and available_tool_slots is not None and available_tool_slots <= 0:
                    await checkpoint_scheduler_fairness()
                    continue

                # Check if we can schedule this thread (sticky scheduling)
                if self.cfg.sticky_scheduling and resource_class == "llm":
                    is_reserved = tid in reserved_slots
                    active_reserved = reserved_slots - set(running_threads.keys())
                    used_slots = running_llm + len(active_reserved)

                    if not is_reserved and used_slots >= self.cfg.effective_max_concurrent_llm_threads:
                        await checkpoint_scheduler_fairness()
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
                await checkpoint_scheduler_fairness()

            try:
                await asyncio.sleep(poll_sec)
            except asyncio.CancelledError:
                await self.shutdown()
                raise

