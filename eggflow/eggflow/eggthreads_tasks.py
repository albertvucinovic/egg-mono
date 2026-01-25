import os
import asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Any, Dict
from .core import Task, Result

# --- Configuration ---
class Config:
    """Configuration for eggthreads integration.

    Attributes:
        EGG_DB_PATH: Path to the eggthreads SQLite database.
        MOCK_MODE: If True, return mock responses without calling LLM.
    """
    EGG_DB_PATH: str = "eggthreads.db"
    MOCK_MODE: bool = True

    @classmethod
    def load(cls):
        """Load configuration from environment variables.

        Reads:
        - EGGFLOW_REAL_LLM: Set to "true" to disable mock mode.
        - EGG_DB_PATH: Override the default database path.
        """
        cls.MOCK_MODE = os.environ.get("EGGFLOW_REAL_LLM", "false").lower() != "true"
        cls.EGG_DB_PATH = os.environ.get("EGG_DB_PATH", "eggthreads.db")

Config.load()

try:
    from eggthreads import db as egg_db_module
    from eggthreads import api as egg_api
    from eggthreads import runner as egg_runner
    from eggthreads.tools import create_default_tools
    EGGTHREADS_INSTALLED = True
except ImportError:
    EGGTHREADS_INSTALLED = False

def get_egg_db():
    if not EGGTHREADS_INSTALLED: return None
    return egg_db_module.ThreadsDB(Config.EGG_DB_PATH)

async def _run_scheduler_until_idle(tid: str):
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE: return
    db = get_egg_db()
    scheduler = egg_runner.SubtreeScheduler(db, tid, tools=create_default_tools())
    task = asyncio.create_task(scheduler.run_forever())
    try:
        await egg_api.wait_subtree_idle(db, tid)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

async def _collect_artifacts(tid: str, files: List[str]):
    if not files: return {}
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE:
        return {f: f"Mock content of {f}" for f in files}
    db = get_egg_db()
    wd = egg_api.get_thread_working_directory(db, tid)
    out = {}
    for f in files:
        p = wd / f
        if p.exists():
            out[f] = p.read_text(errors='replace')
    return out

async def _get_last_content(tid: str):
    if not EGGTHREADS_INSTALLED or Config.MOCK_MODE:
        return f"Mock Reply to prompt"
    db = get_egg_db()
    snap = egg_api.create_snapshot(db, tid)
    msgs = snap.get("messages", []) if isinstance(snap, dict) else []
    return msgs[-1].get("content", "") if msgs else ""

# --- Exceptions ---

class PICRecoveryError(Exception):
    """Raised when thread recovery fails during PIC continuation.

    This error indicates that a thread was in an unhealthy state (e.g., unclosed
    streams, unpublished tool calls) and the recovery attempt via continue_thread
    was unsuccessful.
    """
    pass


class ContextLimitExceededError(Exception):
    """Raised when a thread has exceeded its context limit.

    This is a terminal, non-recoverable error. When a thread hits its context
    limit, the runner emits a system message with the error and the thread fails.
    Unlike other thread failures, this should NOT be "recovered" via continue_thread
    because:
    1. The context limit was set intentionally to bound resource usage
    2. Recovery via summarization loses important context
    3. The thread will likely hit the limit again, causing infinite loops

    The task should propagate this error up and fail gracefully.
    """
    pass


# --- Base Classes ---

class PICTask(Task):
    """Base class for eggthreads tasks with PIC (Persistable Interaction Closure) recovery.

    This base class provides automatic thread health checking and recovery. When an
    eggflow task is re-executed after a crash, the underlying eggthreads Thread may
    be in an unhealthy state (unclosed streams, unpublished tool calls, etc.).

    Subclasses should call `_ensure_thread_healthy(db, thread_id)` at the start of
    their `run()` method to automatically diagnose and recover the thread.

    The recovery is idempotent: calling it on a healthy thread has no effect.
    """

    def _ensure_thread_healthy(self, db, thread_id: str) -> None:
        """Diagnose and recover thread if needed.

        This method should be called at the start of run() for any task that
        operates on an existing thread. It handles the case where eggflow is
        re-executing a task after a crash, and the thread is in a broken state.

        Args:
            db: ThreadsDB instance
            thread_id: The thread to check and potentially recover

        Raises:
            PICRecoveryError: If the thread is unhealthy and recovery fails
            ContextLimitExceededError: If the thread failed due to context limit
                (this is terminal and should NOT be recovered)
        """
        if not EGGTHREADS_INSTALLED:
            return

        diagnosis = egg_api.diagnose_thread(db, thread_id)

        if not diagnosis.is_healthy:
            # CRITICAL: Check for context limit error BEFORE attempting recovery.
            # Context limit exceeded is a terminal error that should NOT be recovered
            # via continue_thread, because:
            # 1. It's an intentional resource bound
            # 2. Recovery via summarization loses context
            # 3. The thread will hit the limit again → infinite loop
            snap = egg_api.create_snapshot(db, thread_id)
            messages = snap.get("messages", []) if isinstance(snap, dict) else []

            # Check recent system messages for context limit error
            for msg in reversed(messages[-5:]):  # Check last 5 messages
                if msg.get('role') == 'system':
                    content = msg.get('content', '').lower()
                    # Match both our internal "context limit" error and API "context size" errors
                    is_context_error = (
                        ('context limit' in content and 'error' in content) or
                        ('context size' in content and 'exceed' in content) or
                        ('exceed_context_size_error' in content)
                    )
                    if is_context_error:
                        raise ContextLimitExceededError(
                            f"Thread {thread_id} exceeded context limit. "
                            f"This is terminal and cannot be recovered. "
                            f"Error: {msg.get('content')}"
                        )

            # Use continue_thread to recover the thread state.
            # continue_thread handles expired leases (from heartbeat timeout) automatically,
            # so we don't need to call interrupt_thread first.
            result = egg_api.continue_thread(
                db,
                thread_id,
                diagnosis.suggested_continue_point
            )
            if not result.success:
                raise PICRecoveryError(
                    f"Failed to recover thread {thread_id}: {result.message}. "
                    f"Issues detected: {diagnosis.issues}"
                )


# --- Result Types ---

@dataclass
class ThreadResult:
    """Result from a thread task execution.

    Attributes:
        thread_id: The eggthreads thread ID.
        content: The assistant's final response content.
        artifacts: Dict mapping filenames to file contents extracted from working dir.
    """
    thread_id: str
    content: str
    artifacts: Dict[str, str] = field(default_factory=dict)

@dataclass
class CreateThread(PICTask):
    """Create a new eggthreads thread and run it to completion.

    This task creates a root thread, adds a user message, runs the scheduler
    until idle, and returns the assistant's response.

    Inherits from PICTask to provide automatic crash recovery. If the task is
    re-executed after a crash (e.g., crashed during LLM streaming), the thread
    will be automatically diagnosed and recovered before proceeding.

    Attributes:
        prompt: The user message to send.
        model_key: Optional model key for the thread (e.g., "gpt-4", "claude-3").
        system_prompt: Optional system prompt to set.
        seed: Random seed for reproducibility in cache key.
        output_files: List of filenames to extract from working directory.
    """
    prompt: str
    model_key: Optional[str] = None
    system_prompt: Optional[str] = None
    seed: int = 0
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> ThreadResult:
        """Execute the thread creation and return the result."""
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            tid = f"mock_{abs(hash(self.prompt))}"
            artifacts = await _collect_artifacts(tid, self.output_files)
            return ThreadResult(thread_id=tid, content=f"Mock Reply to '{self.prompt}'", artifacts=artifacts)

        db = get_egg_db()

        # Check if we're resuming from a previous attempt that created the thread
        # but crashed before completing. Look for existing thread with our prompt.
        # For now, always create new - the cache key ensures we don't duplicate.
        tid = egg_api.create_root_thread(db, initial_model_key=self.model_key)

        # Ensure thread is healthy (handles crash during previous execution)
        self._ensure_thread_healthy(db, tid)

        # Idempotent message append: check if prompt already exists
        snap = egg_api.create_snapshot(db, tid)
        msgs = snap.get("messages", []) if isinstance(snap, dict) else []
        has_prompt = any(
            m.get('role') == 'user' and m.get('content') == self.prompt
            for m in msgs
        )

        if not has_prompt:
            egg_api.append_message(db, tid, "user", self.prompt)

        await _run_scheduler_until_idle(tid)

        content = await _get_last_content(tid)
        artifacts = await _collect_artifacts(tid, self.output_files)
        return ThreadResult(thread_id=tid, content=content, artifacts=artifacts)

@dataclass
class ContinueThread(PICTask):
    """Continue an existing thread with a new message.

    Appends a message to an existing thread (if not already present),
    runs the scheduler until idle, and returns the response.

    Inherits from PICTask to provide automatic crash recovery. If the task is
    re-executed after a crash, the thread will be automatically diagnosed and
    recovered before proceeding. This handles cases like:
    - Crash during LLM streaming (unclosed streams)
    - Crash during tool execution (unpublished tool calls)
    - Crash after message append but before scheduler completion

    Attributes:
        thread_id: The thread to continue.
        content: Message content to append.
        role: Message role (default "user").
        output_files: List of filenames to extract from working directory.
    """
    thread_id: str
    content: str
    role: str = "user"
    output_files: List[str] = field(default_factory=list)

    async def run(self) -> ThreadResult:
        """Execute the thread continuation and return the result."""
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            artifacts = await _collect_artifacts(self.thread_id, self.output_files)
            return ThreadResult(thread_id=self.thread_id, content=f"Mock Reply to '{self.content}'", artifacts=artifacts)

        db = get_egg_db()

        # CRITICAL: Ensure thread is healthy before proceeding.
        # This handles crash recovery when eggflow re-executes this task.
        self._ensure_thread_healthy(db, self.thread_id)

        # Idempotent message append: only add if not already present
        snap = egg_api.create_snapshot(db, self.thread_id)
        msgs = snap.get("messages", []) if isinstance(snap, dict) else []
        last = msgs[-1] if msgs else None

        if not (last and last.get('role') == self.role and last.get('content') == self.content):
            egg_api.append_message(db, self.thread_id, self.role, self.content)

        await _run_scheduler_until_idle(self.thread_id)
        content = await _get_last_content(self.thread_id)
        artifacts = await _collect_artifacts(self.thread_id, self.output_files)
        return ThreadResult(thread_id=self.thread_id, content=content, artifacts=artifacts)

@dataclass
class ForkThread(PICTask):
    """Fork (duplicate) an existing thread to create a branch.

    Creates a copy of the source thread's conversation history,
    allowing independent continuation from that point.

    Inherits from PICTask to provide automatic crash recovery. Ensures the
    source thread is healthy before forking, which prevents duplicating
    a thread in a broken state.

    Attributes:
        source_thread_id: The thread to duplicate.
    """
    source_thread_id: str

    async def run(self) -> str:
        """Execute the fork and return the new thread ID."""
        if Config.MOCK_MODE or not EGGTHREADS_INSTALLED:
            return f"{self.source_thread_id}_fork"

        db = get_egg_db()

        # Ensure source thread is healthy before forking
        # This prevents duplicating a thread with unclosed streams or
        # unpublished tool calls
        self._ensure_thread_healthy(db, self.source_thread_id)

        await egg_api.wait_subtree_idle(db, self.source_thread_id)
        return egg_api.duplicate_thread(db, self.source_thread_id)
