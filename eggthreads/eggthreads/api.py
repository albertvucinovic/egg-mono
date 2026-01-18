from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .db import ThreadsDB, ThreadRow
from .snapshot import SnapshotBuilder
from .runner import ThreadRunner, RunnerConfig

try:
    from eggllm.config import load_models_config
    from eggllm.registry import ModelRegistry
    from eggllm.catalog import AllModelsCatalog
    EGGLLM_AVAILABLE = True
except ImportError:
    try:
        from eggllm.eggllm.config import load_models_config
        from eggllm.eggllm.registry import ModelRegistry
        from eggllm.eggllm.catalog import AllModelsCatalog
        EGGLLM_AVAILABLE = True
    except ImportError:
        EGGLLM_AVAILABLE = False


def _get_default_model_key(models_path: str = "models.json") -> Optional[str]:
    """Return the default_model key from models.json, or None if unavailable."""
    import os.path
    if not os.path.exists(models_path):
        return None
    try:
        with open(models_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            dm = data.get('default_model')
            if isinstance(dm, str) and dm.strip():
                return dm.strip()
    except Exception:
        pass
    return None


def _get_concrete_model_info(model_key: str, models_path: str = "models.json"):
    """Return nested providers dict for a given model key.
    
    Raises ValueError if model_key not found or eggllm not available.
    """
    # First try eggllm if available
    try:
        from eggllm.config import load_models_config
        from eggllm.registry import ModelRegistry
        from eggllm.catalog import AllModelsCatalog
    except ImportError:
        try:
            from eggllm.eggllm.config import load_models_config
            from eggllm.eggllm.registry import ModelRegistry
            from eggllm.eggllm.catalog import AllModelsCatalog
        except ImportError:
            # eggllm not available, fall back to direct parsing
            load_models_config = None
    
    if load_models_config is not None:
        try:
            models_config, providers_config = load_models_config(models_path)
            catalog = AllModelsCatalog(None)  # dummy catalog
            registry = ModelRegistry(models_config, providers_config, catalog)
            if model_key not in models_config:
                # try to resolve aliases
                resolved = registry.resolve(model_key)
                if resolved is None:
                    raise ValueError(f"Model key '{model_key}' not found in {models_path}")
                model_key = resolved
            return registry.get_concrete_model_info(model_key)
        except Exception:
            # eggllm path failed, fall through to direct parsing
            pass
    
    # Fallback: parse models.json directly
    import json
    import os.path
    try:
        with open(models_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        raise ValueError(f"Cannot read models file: {models_path}")
    if not isinstance(data, dict):
        raise ValueError(f"Invalid models file: {models_path}")
    # New format with providers
    if "providers" in data and isinstance(data["providers"], dict):
        providers = data["providers"]
        for provider_name, provider_cfg in providers.items():
            if not isinstance(provider_cfg, dict):
                continue
            models_map = provider_cfg.get("models", {})
            if not isinstance(models_map, dict):
                continue
            if model_key in models_map:
                model_cfg = models_map[model_key]
                if not isinstance(model_cfg, dict):
                    model_cfg = {}
                provider_dict = {}
                if "api_base" in provider_cfg:
                    provider_dict["api_base"] = provider_cfg["api_base"]
                if "api_key_env" in provider_cfg:
                    provider_dict["api_key_env"] = provider_cfg["api_key_env"]
                if "parameters" in provider_cfg and isinstance(provider_cfg["parameters"], dict):
                    provider_dict["parameters"] = provider_cfg["parameters"]
                model_dict = {k: v for k, v in model_cfg.items() if k != "provider"}
                if "model_name" not in model_dict:
                    model_dict["model_name"] = model_key
                return {
                    "providers": {
                        provider_name: {
                            **provider_dict,
                            "models": {
                                model_key: model_dict
                            }
                        }
                    }
                }
        raise ValueError(f"Model key '{model_key}' not found in {models_path}")
    # Old flat format not supported
    raise ValueError(f"Model key '{model_key}' not found in {models_path}")

def _ulid_like() -> str:
    # Real ULID using Crockford's Base32. Minimal local implementation to avoid extra deps.
    import os, time
    ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    t = int(time.time() * 1000)
    def enc128(x: int, n: int) -> str:
        out = []
        for _ in range(n):
            out.append(ENCODING[x & 31])
            x >>= 5
        return ''.join(reversed(out))
    # 48-bit timestamp -> 10 chars; 80-bit randomness -> 16 chars
    ts = enc128(t, 10)
    rand = int.from_bytes(os.urandom(10), 'big')
    rd = enc128(rand, 16)
    return ts + rd


def create_root_thread(db: ThreadsDB, name: Optional[str] = None, initial_model_key: Optional[str] = None,
                       models_path: str = "models.json") -> str:
    """Create a new root thread (top-level conversation).

    A root thread has no parent and serves as the entry point for a
    conversation tree. Child threads can be branched from it using
    ``create_child_thread()``.

    Args:
        db: ThreadsDB instance for database operations.
        name: Optional human-readable name for the thread.
        initial_model_key: Model key to use for this thread. If None,
            defaults to the ``default_model`` from models.json.
        models_path: Path to models.json configuration file.

    Returns:
        The new thread's unique ID (ULID format).
    """
    tid = _ulid_like()

    # If no initial_model_key is provided, default to the default_model from models.json
    effective_model_key = initial_model_key
    if effective_model_key is None:
        effective_model_key = _get_default_model_key(models_path)

    db.create_thread(thread_id=tid, name=name, parent_id=None, initial_model_key=effective_model_key, depth=0)

    # Emit model.switch event with concrete_model_info if we have a model
    if effective_model_key:
        set_thread_model(db, tid, effective_model_key, reason='initial', models_path=models_path)
    return tid


def create_child_thread(db: ThreadsDB, parent_id: str, name: Optional[str] = None, initial_model_key: Optional[str] = None,
                        models_path: str = "models.json") -> str:
    """Create a child thread branching from a parent thread.

    Child threads inherit the parent's model configuration by default
    and are tracked in the parent-child relationship for subtree
    operations.

    Args:
        db: ThreadsDB instance for database operations.
        parent_id: ID of the parent thread to branch from.
        name: Optional human-readable name for the thread.
        initial_model_key: Model key to use for this thread. If None,
            inherits from the parent thread's current model.
        models_path: Path to models.json configuration file.

    Returns:
        The new child thread's unique ID (ULID format).
    """
    parent = db.get_thread(parent_id)
    depth = (parent.depth + 1) if parent else 1
    tid = _ulid_like()
    db.create_thread(thread_id=tid, name=name, parent_id=parent_id, initial_model_key=initial_model_key, depth=depth)

    # Model inheritance: if initial_model_key is explicitly provided, use it.
    # Otherwise, inherit from parent's model.switch event (including concrete_model_info).
    if initial_model_key:
        # Explicit model specified - look it up from models_path
        set_thread_model(db, tid, initial_model_key, reason='initial', models_path=models_path)
    else:
        # Inherit from parent: copy parent's model.switch event with concrete_model_info
        parent_model = current_thread_model(db, parent_id)
        if parent_model:
            parent_concrete = current_thread_model_info(db, parent_id)
            # Create model.switch event with inherited concrete_model_info (no models_path lookup needed)
            set_thread_model(db, tid, parent_model, reason='inherited', concrete_model_info=parent_concrete)

    # Do not eagerly persist sandbox configuration on the child.
    # The effective sandbox config is resolved by inheriting the nearest
    # ancestor's sandbox.config event at execution time.

    return tid


def duplicate_thread(db: ThreadsDB, source_thread_id: str, name: Optional[str] = None) -> str:
    """Duplicate a thread's event log into a new root thread.

    This creates a new *root* thread whose events and snapshot are a
    copy of ``source_thread_id`` at the time of invocation. The new
    thread shares no open stream with the original (no rows are added
    to ``open_streams``) but otherwise has identical history: all
    ``msg.create``, ``stream.*``, and ``tool_call.*`` events are
    replayed with fresh event_ids, preserving msg_id and invoke_id so
    that runner/actionable semantics (RA1/RA2/RA3, tool states, etc.)
    behave as if the thread had been executed separately.

    The duplicate is intended as a "checkpoint" copy: a frozen backup
    of the conversation that can be inspected or resumed independently
    of the original.
    """

    # Look up source metadata to derive a sensible name and model.
    src = db.get_thread(source_thread_id)
    if not src:
        raise ValueError(f"Source thread not found: {source_thread_id}")

    base_name = src.name or src.short_recap or "Thread"
    new_name = name or f"{base_name} [copy]"

    # Always create the duplicate as a new root thread so it is
    # independent from any existing parent/children relationships.
    new_tid = _ulid_like()
    db.create_thread(
        thread_id=new_tid,
        name=new_name,
        parent_id=None,
        initial_model_key=src.initial_model_key,
        depth=0,
    )

    # Replay msg.create and tool_call.* events from the source thread.
    # Skip streaming events, control events, and edit events to get a
    # clean conversation copy without RA1 boundary markers or skip flags.
    import json as _json

    # First, collect msg_ids that have been marked as skipped via msg.edit events
    # (skipped_on_continue is stored in msg.edit, not msg.create)
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
        (source_thread_id,),
    )
    for row in cur_edit.fetchall():
        edit_msg_id = row[0]
        try:
            edit_payload = _json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            edit_payload = {}
        if edit_payload.get('skipped_on_continue'):
            skipped_msg_ids.add(edit_msg_id)

    cur = db.conn.execute(
        "SELECT type, msg_id, invoke_id, chunk_seq, payload_json FROM events WHERE thread_id=? ORDER BY event_seq ASC",
        (source_thread_id,),
    )
    rows = cur.fetchall()
    for ev_type, msg_id, invoke_id, chunk_seq, pj in rows:
        # Only copy message and tool_call events for a clean duplicate
        if not (ev_type == 'msg.create' or ev_type.startswith('tool_call.')):
            continue
        # Don't copy messages marked as skipped in the source
        if ev_type == 'msg.create' and msg_id in skipped_msg_ids:
            continue
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_=ev_type,
            payload=payload,
            msg_id=msg_id,
            invoke_id=invoke_id,
            chunk_seq=chunk_seq,
        )

    # Build a fresh snapshot for the duplicate so UIs and runners see a
    # consistent cached view of messages.
    create_snapshot(db, new_tid)
    return new_tid


def duplicate_thread_up_to(db: ThreadsDB, source_thread_id: str, up_to_msg_id: str, name: Optional[str] = None) -> str:
    """Duplicate a thread's event log up to a specific message.

    Like duplicate_thread, but only copies events up to and including the
    message with the given msg_id. This is useful for creating a checkpoint
    at a specific point in the conversation.

    Args:
        db: ThreadsDB instance
        source_thread_id: Thread to duplicate
        up_to_msg_id: Message ID to stop at (inclusive)
        name: Optional name for the new thread

    Returns:
        The new thread's ID
    """
    src = db.get_thread(source_thread_id)
    if not src:
        raise ValueError(f"Source thread not found: {source_thread_id}")

    # Find the event_seq of the target message
    cur = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? ORDER BY event_seq ASC LIMIT 1",
        (source_thread_id, up_to_msg_id),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Message not found: {up_to_msg_id}")
    up_to_seq = row[0]

    base_name = src.name or src.short_recap or "Thread"
    new_name = name or f"{base_name} [copy]"

    new_tid = _ulid_like()
    db.create_thread(
        thread_id=new_tid,
        name=new_name,
        parent_id=None,
        initial_model_key=src.initial_model_key,
        depth=0,
    )

    import json as _json

    # Only copy msg.create and tool_call.* events to get a clean conversation.
    # Skip streaming events, control events, and edit events to avoid
    # carrying over RA1 boundary markers or skip flags from the source.

    # First, collect msg_ids that have been marked as skipped via msg.edit events
    # (skipped_on_continue is stored in msg.edit, not msg.create)
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
        (source_thread_id,),
    )
    for row in cur_edit.fetchall():
        edit_msg_id = row[0]
        try:
            edit_payload = _json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            edit_payload = {}
        if edit_payload.get('skipped_on_continue'):
            skipped_msg_ids.add(edit_msg_id)

    cur = db.conn.execute(
        "SELECT type, msg_id, invoke_id, chunk_seq, payload_json FROM events "
        "WHERE thread_id=? AND event_seq <= ? ORDER BY event_seq ASC",
        (source_thread_id, up_to_seq),
    )
    rows = cur.fetchall()
    for ev_type, msg_id, invoke_id, chunk_seq, pj in rows:
        # Only copy message and tool_call events for a clean duplicate
        if not (ev_type == 'msg.create' or ev_type.startswith('tool_call.')):
            continue
        # Don't copy messages marked as skipped in the source
        if ev_type == 'msg.create' and msg_id in skipped_msg_ids:
            continue
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_=ev_type,
            payload=payload,
            msg_id=msg_id,
            invoke_id=invoke_id,
            chunk_seq=chunk_seq,
        )

    create_snapshot(db, new_tid)
    return new_tid


# --------- Continue Thread API ------------------------------------------------

@dataclass
class ContinueResult:
    """Result of continue_thread operation."""
    success: bool
    continue_from_msg_id: Optional[str]
    skipped_msg_ids: List[str]
    message: str
    diagnosis: Optional['ThreadDiagnosis'] = None


def _get_event_seq_for_msg_id(db: ThreadsDB, thread_id: str, msg_id: str) -> Optional[int]:
    """Get the event_seq for a message ID."""
    cur = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create' ORDER BY event_seq ASC LIMIT 1",
        (thread_id, msg_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


@dataclass
class ThreadDiagnosis:
    """Diagnosis of thread state for auto-fix."""
    is_healthy: bool
    issues: List[str]
    suggested_continue_point: Optional[str]
    details: Dict[str, Any]


def diagnose_thread(db: ThreadsDB, thread_id: str) -> ThreadDiagnosis:
    """Diagnose thread state and suggest fixes.

    Checks for common issues:
    1. Unclosed streams (interrupted streaming)
    2. Unpublished tool calls (incomplete tool execution)
    3. Consecutive assistant messages (API will reject)
    4. Error messages at the end
    5. Thread stuck in unexpected state

    Returns a diagnosis with suggested continue point to fix issues.
    """
    from .tool_state import build_tool_call_states

    issues: List[str] = []
    details: Dict[str, Any] = {}

    # Check for unclosed streams
    cur = db.conn.execute("""
        SELECT type, event_seq, invoke_id FROM events
        WHERE thread_id = ? AND type IN ('stream.open', 'stream.close')
        ORDER BY event_seq ASC
    """, (thread_id,))
    stream_events = cur.fetchall()

    open_streams: Dict[str, int] = {}
    for ev_type, ev_seq, inv_id in stream_events:
        if ev_type == 'stream.open':
            open_streams[inv_id] = ev_seq
        elif ev_type == 'stream.close' and inv_id in open_streams:
            del open_streams[inv_id]

    if open_streams:
        issues.append(f"Unclosed streams: {len(open_streams)} stream(s) were interrupted")
        details['unclosed_streams'] = list(open_streams.keys())

    # Check for unpublished tool calls
    tc_states = build_tool_call_states(db, thread_id)
    unpublished = [tc for tc in tc_states.values() if not tc.published]
    if unpublished:
        issues.append(f"Unpublished tool calls: {len(unpublished)} tool call(s) pending")
        details['unpublished_tool_calls'] = [tc.tool_call_id for tc in unpublished]

    # First, collect msg_ids that have been marked as skipped via msg.edit events
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
        (thread_id,),
    )
    for row in cur_edit.fetchall():
        edit_msg_id = row[0]
        try:
            edit_payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            edit_payload = {}
        if edit_payload.get('skipped_on_continue'):
            skipped_msg_ids.add(edit_msg_id)

    # Get messages in order, excluding already-skipped messages
    cur = db.conn.execute(
        "SELECT msg_id, payload_json, event_seq FROM events "
        "WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (thread_id,),
    )
    messages = []
    for msg_id, pj, ev_seq in cur.fetchall():
        # Skip messages that were already marked as skipped
        if msg_id in skipped_msg_ids:
            continue
        try:
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        messages.append((msg_id, payload, ev_seq))

    # Check for consecutive assistant messages
    # Build a list of (index, msg_id) for assistant messages
    assistant_indices = []
    for i, (msg_id, payload, ev_seq) in enumerate(messages):
        role = payload.get('role')
        if role == 'assistant':
            assistant_indices.append((i, msg_id))

    # Find consecutive assistant pairs
    consecutive_assistants = []
    first_consecutive_index = None
    for j in range(1, len(assistant_indices)):
        prev_idx, prev_mid = assistant_indices[j - 1]
        curr_idx, curr_mid = assistant_indices[j]
        # Check if they're truly consecutive in the messages list (adjacent indices)
        if curr_idx == prev_idx + 1:
            consecutive_assistants.append(curr_mid)
            if first_consecutive_index is None:
                first_consecutive_index = prev_idx  # Index of first assistant in sequence

    # Find the message before the first consecutive assistant
    first_consecutive_assistant_msg_id = None
    msg_before_first_consecutive = None
    if consecutive_assistants and first_consecutive_index is not None:
        first_consecutive_assistant_msg_id = messages[first_consecutive_index][0]
        # The message before the first consecutive assistant
        if first_consecutive_index > 0:
            msg_before_first_consecutive = messages[first_consecutive_index - 1][0]

    if consecutive_assistants:
        issues.append(f"Consecutive assistant messages: {len(consecutive_assistants)} occurrence(s)")
        details['consecutive_assistants'] = consecutive_assistants
        details['first_consecutive_assistant'] = first_consecutive_assistant_msg_id
        details['msg_before_consecutive'] = msg_before_first_consecutive

    # Check for error messages at the end
    if messages:
        last_msg_id, last_payload, _ = messages[-1]
        last_role = last_payload.get('role')
        last_content = last_payload.get('content', '')
        if last_role == 'system' and 'error' in last_content.lower():
            issues.append("Thread ends with error message")
            details['last_error'] = last_content[:200]

    # Determine if thread is healthy and find continue point
    is_healthy = len(issues) == 0
    suggested_point = None

    if not is_healthy:
        # For consecutive assistants, we need to continue from BEFORE the first
        # consecutive assistant to remove all consecutive assistant messages
        if consecutive_assistants and msg_before_first_consecutive:
            suggested_point = msg_before_first_consecutive
        else:
            # Fall back to general continue point detection
            suggested_point = find_continue_point(db, thread_id)

    return ThreadDiagnosis(
        is_healthy=is_healthy,
        issues=issues,
        suggested_continue_point=suggested_point,
        details=details,
    )


def find_continue_point(db: ThreadsDB, thread_id: str) -> Optional[str]:
    """Auto-detect the best msg_id to continue from.

    Searches backward through the thread to find an appropriate point to resume.
    The algorithm prioritizes finding a stable state:

    1. After the last published tool result (TC6 state) - safest point
    2. After the last complete assistant message (with no pending tool calls)
    3. After the last user message that doesn't have keep_user_turn

    Returns:
        The msg_id to continue from, or None if the thread should continue
        from the very beginning (no messages to skip).
    """
    from .tool_state import build_tool_call_states

    # First, collect msg_ids that have been marked as skipped via msg.edit events
    skipped_msg_ids: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
        (thread_id,),
    )
    for row in cur_edit.fetchall():
        edit_msg_id = row[0]
        try:
            edit_payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            edit_payload = {}
        if edit_payload.get('skipped_on_continue'):
            skipped_msg_ids.add(edit_msg_id)

    # Get all messages in reverse order
    cur = db.conn.execute(
        "SELECT msg_id, payload_json, event_seq FROM events "
        "WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC",
        (thread_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    # Build tool call states to understand pending work
    tc_states = build_tool_call_states(db, thread_id)

    # Find messages with unpublished tool calls
    unpublished_tc_msg_ids = set()
    for tc in tc_states.values():
        if not tc.published:
            unpublished_tc_msg_ids.add(tc.parent_msg_id)

    # Iterate backward to find a good continue point
    for msg_id, pj, event_seq in rows:
        # Skip already-skipped messages (check msg.edit events, not msg.create payload)
        if msg_id in skipped_msg_ids:
            continue

        try:
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}

        role = payload.get('role')
        no_api = payload.get('no_api')
        keep_user_turn = payload.get('keep_user_turn')

        # Skip no_api messages (they don't participate in RA1)
        if no_api:
            continue

        # Check if this message has unpublished tool calls - skip it
        if msg_id in unpublished_tc_msg_ids:
            continue

        # A tool message (TC6) is a safe continue point
        if role == 'tool':
            return msg_id

        # An assistant message without pending tool calls is a good point
        if role == 'assistant':
            tool_calls = payload.get('tool_calls', [])
            if not tool_calls:
                # No tool calls - safe point
                return msg_id
            # Has tool calls - check if all are published
            all_published = True
            for tc in tool_calls:
                tc_id = tc.get('id')
                if tc_id and tc_id in tc_states:
                    if not tc_states[tc_id].published:
                        all_published = False
                        break
            if all_published:
                return msg_id
            # Some tool calls not published - keep looking

        # A user message without keep_user_turn is a continue point
        if role == 'user' and not keep_user_turn:
            tool_calls = payload.get('tool_calls', [])
            if not tool_calls:
                return msg_id
            # User message with tool_calls - check if all published
            all_published = True
            for tc in tool_calls:
                tc_id = tc.get('id')
                if tc_id and tc_id in tc_states:
                    if not tc_states[tc_id].published:
                        all_published = False
                        break
            if all_published:
                return msg_id

    # No good continue point found - return None (start from beginning)
    return None


def is_thread_continuable(db: ThreadsDB, thread_id: str) -> bool:
    """Check if a thread can be continued.

    A thread is continuable if:
    - It exists
    - It is not currently running (no active, non-expired open_streams lease)
    - There are messages after the last RA1 boundary that can be skipped

    Note: A thread in 'waiting_user' state is technically continuable,
    but /continue would effectively be a no-op.
    """
    th = db.get_thread(thread_id)
    if not th:
        return False

    # Check if there's an active lease (not expired)
    try:
        row = db.current_open(thread_id)
        if row:
            from datetime import datetime
            lease_until = row['lease_until']
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            if lease_until and lease_until > now_iso:
                return False  # Thread is running (lease still valid)
            # Lease has expired - thread not actually running
    except Exception:
        pass

    return True


def continue_thread(
    db: ThreadsDB,
    thread_id: str,
    msg_id: Optional[str] = None,
) -> ContinueResult:
    """Continue a thread from a specific point or auto-detected continue point.

    This function marks messages after the continue point with `skipped_on_continue=True`
    via msg.edit events. The RA1 detection will ignore these messages, allowing the
    thread to be re-run from the continue point.

    Args:
        db: ThreadsDB instance
        thread_id: Thread to continue
        msg_id: Optional message ID to continue from. If None, auto-detect.

    Returns:
        ContinueResult with details of the operation
    """
    th = db.get_thread(thread_id)
    if not th:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message=f"Thread not found: {thread_id}"
        )

    # Check if thread is running (has an active, non-expired lease)
    try:
        row = db.current_open(thread_id)
        if row:
            from datetime import datetime
            lease_until = row['lease_until']
            now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
            # Only block if the lease hasn't expired yet
            if lease_until and lease_until > now_iso:
                return ContinueResult(
                    success=False,
                    continue_from_msg_id=None,
                    skipped_msg_ids=[],
                    message="Thread is currently running. Interrupt it first."
                )
            # Lease has expired - thread is not actually running, proceed with continue
    except Exception:
        pass

    # Determine the continue point
    diagnosis = None
    if msg_id is None:
        # Run diagnosis to understand thread state and auto-detect continue point
        diagnosis = diagnose_thread(db, thread_id)
        if diagnosis.is_healthy:
            return ContinueResult(
                success=True,
                continue_from_msg_id=None,
                skipped_msg_ids=[],
                message="Thread is healthy. No changes needed.",
                diagnosis=diagnosis,
            )
        # Use suggested continue point from diagnosis
        msg_id = diagnosis.suggested_continue_point
        if msg_id is None:
            return ContinueResult(
                success=True,
                continue_from_msg_id=None,
                skipped_msg_ids=[],
                message="No messages to skip. Thread can continue from current state.",
                diagnosis=diagnosis,
            )

    # Get the event_seq for the continue point
    continue_seq = _get_event_seq_for_msg_id(db, thread_id, msg_id)
    if continue_seq is None:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message=f"Message not found: {msg_id}"
        )

    # First, collect msg_ids that have already been marked as skipped via msg.edit events
    already_skipped: set = set()
    cur_edit = db.conn.execute(
        "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
        (thread_id,),
    )
    for row in cur_edit.fetchall():
        edit_msg_id = row[0]
        try:
            edit_payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        except Exception:
            edit_payload = {}
        if edit_payload.get('skipped_on_continue'):
            already_skipped.add(edit_msg_id)

    # Find all messages after the continue point
    cur = db.conn.execute(
        "SELECT msg_id, payload_json FROM events "
        "WHERE thread_id=? AND type='msg.create' AND event_seq > ? ORDER BY event_seq ASC",
        (thread_id, continue_seq),
    )
    rows = cur.fetchall()

    skipped_msg_ids = []
    continue_event_id = _ulid_like()  # Shared ID to link all edits

    for row_msg_id, pj in rows:
        if row_msg_id is None:
            continue

        # Skip already-skipped messages (check msg.edit events, not msg.create payload)
        if row_msg_id in already_skipped:
            continue

        # Mark this message as skipped
        db.append_event(
            event_id=_ulid_like(),
            thread_id=thread_id,
            type_='msg.edit',
            payload={
                'skipped_on_continue': True,
                'continue_event_id': continue_event_id,
            },
            msg_id=row_msg_id,
        )
        skipped_msg_ids.append(row_msg_id)

    # Also add a control.interrupt event to advance the RA1 boundary
    # This ensures the runner doesn't try to continue from the old state
    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='control.interrupt',
        payload={
            'reason': 'continue_thread',
            'old_invoke_id': None,
            'new_invoke_id': _ulid_like(),
            'purpose': 'continue',
            'continue_from_msg_id': msg_id,
            'skipped_count': len(skipped_msg_ids),
        },
    )

    # Rebuild snapshot to reflect the skipped messages
    create_snapshot(db, thread_id)

    # Build informative message
    base_msg = f"Continued from message {msg_id[-8:] if msg_id else 'start'}, skipped {len(skipped_msg_ids)} messages."
    if diagnosis and diagnosis.issues:
        base_msg = f"Fixed {len(diagnosis.issues)} issue(s): {', '.join(diagnosis.issues)}. {base_msg}"

    return ContinueResult(
        success=True,
        continue_from_msg_id=msg_id,
        skipped_msg_ids=skipped_msg_ids,
        message=base_msg,
        diagnosis=diagnosis,
    )


async def continue_thread_async(
    db: ThreadsDB,
    thread_id: str,
    msg_id: Optional[str] = None,
    delay_sec: Optional[float] = None,
) -> ContinueResult:
    """Async version of continue_thread with optional delay.

    If delay_sec is specified, waits for the specified time before applying
    the continue operation. This is useful for API rate limit scenarios where
    you want to retry after a delay.

    Args:
        db: ThreadsDB instance
        thread_id: Thread to continue
        msg_id: Optional message ID to continue from
        delay_sec: Optional delay in seconds before applying the continue.
                   The thread will be picked up by the runner after this delay.

    Returns:
        ContinueResult with details of the operation
    """
    import asyncio

    # If delay requested, wait before applying the continue
    if delay_sec is not None and delay_sec > 0:
        await asyncio.sleep(delay_sec)

    # Now apply the continue
    result = continue_thread(db, thread_id, msg_id)
    if result.success and delay_sec is not None and delay_sec > 0:
        result.message = f"After {delay_sec}s delay: {result.message}"

    return result


def append_message(db: ThreadsDB, thread_id: str, role: str, content: str, extra: Optional[Dict[str, Any]] = None) -> str:
    """Append a user/assistant/system message to a thread.

    This helper is intentionally thin: policy decisions about which
    messages are sent to the provider (e.g. via ``no_api``) are handled
    elsewhere, primarily in ``thread_state`` / ``discover_runner_actionable``
    and ``ThreadRunner._sanitize_messages_for_api``.
    """

    payload_extra: Dict[str, Any] = dict(extra or {})

    msg_id = _ulid_like()
    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='msg.create',
        payload={"role": role, "content": content, **payload_extra},
        msg_id=msg_id,
    )
    return msg_id


def edit_message(db: ThreadsDB, thread_id: str, msg_id: str, new_content: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """Edit an existing message's content.

    Appends a ``msg.edit`` event that updates the message content.
    The original message is preserved in the event log for audit purposes.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread containing the message.
        msg_id: ID of the message to edit.
        new_content: New content to replace the existing content.
        extra: Optional additional payload fields for the edit event.
    """
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='msg.edit', payload={"content": new_content, **(extra or {})}, msg_id=msg_id)


def delete_message(db: ThreadsDB, thread_id: str, msg_id: str) -> None:
    """Mark a message as deleted.

    Appends a ``msg.delete`` event. The snapshot builder interprets
    this to exclude the message from the reconstructed conversation.
    The original message remains in the event log for audit purposes.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread containing the message.
        msg_id: ID of the message to delete.
    """
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='msg.delete', payload={"reason": "user"}, msg_id=msg_id)


def create_snapshot(db: ThreadsDB, thread_id: str) -> str:
    """Rebuild and persist the thread snapshot from events.

    Processes all events for the thread and constructs a snapshot
    representing the current conversation state. The snapshot is
    stored in the threads table for fast access.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread to snapshot.

    Returns:
        The snapshot JSON string.
    """
    cur = db.conn.execute("SELECT * FROM events WHERE thread_id=? ORDER BY event_seq ASC", (thread_id,))
    evs = cur.fetchall()
    builder = SnapshotBuilder()
    snap = builder.build(evs)
    last_seq = evs[-1]["event_seq"] if evs else -1
    db.conn.execute("UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?",
                    (json.dumps(snap), last_seq, thread_id))
    return snap


def delete_thread(db: ThreadsDB, thread_id: str) -> None:
    """Delete a thread and cascade related rows via foreign keys.

    Removes the thread from threads; ON DELETE CASCADE removes
    - children rows that reference it (as parent or child)
    - events rows for the thread
    - open_streams row for the thread
    """
    db.conn.execute("DELETE FROM threads WHERE thread_id=?", (thread_id,))


def is_thread_runnable(db: ThreadsDB, thread_id: str) -> bool:
    """Public API to check if a thread is runnable.

    This now delegates to discover_runner_actionable so that the
    ThreadRunner and external callers share the same notion of
    runnable work (RA1/RA2/RA3).
    """
    from .tool_state import discover_runner_actionable_cached

    return discover_runner_actionable_cached(db, thread_id) is not None


# --------- Query helpers (expose common SQL as API) -------------------------
def list_threads(db: ThreadsDB) -> list[ThreadRow]:
    """List all threads in the database.

    Args:
        db: ThreadsDB instance for database operations.

    Returns:
        List of ThreadRow objects for all threads.
    """
    try:
        cur = db.conn.execute("SELECT * FROM threads")
        rows = [ThreadRow(**dict(r)) for r in cur.fetchall()]
    except Exception:
        rows = []
    return rows


def list_root_threads(db: ThreadsDB) -> list[str]:
    """List all root threads (threads with no parent).

    Root threads are top-level conversations that were created with
    ``create_root_thread()`` and are not children of any other thread.

    Args:
        db: ThreadsDB instance for database operations.

    Returns:
        List of thread IDs for all root threads.
    """
    try:
        cur = db.conn.execute("SELECT thread_id FROM threads WHERE thread_id NOT IN (SELECT child_id FROM children)")
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def get_parent(db: ThreadsDB, child_id: str) -> Optional[str]:
    """Get the parent thread ID for a child thread.

    Args:
        db: ThreadsDB instance for database operations.
        child_id: ID of the child thread.

    Returns:
        Parent thread ID, or None if the thread is a root thread
        or doesn't exist.
    """
    try:
        row = db.conn.execute('SELECT parent_id FROM children WHERE child_id=?', (child_id,)).fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def list_children_with_meta(db: ThreadsDB, parent_id: str) -> list[tuple[str, str, str, str]]:
    """Return list of (child_id, name, short_recap, created_at) for a parent."""
    try:
        cur = db.conn.execute(
            "SELECT c.child_id, t.name, t.short_recap, t.created_at FROM children c JOIN threads t ON t.thread_id=c.child_id WHERE c.parent_id=? ORDER BY t.created_at ASC",
            (parent_id,)
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    except Exception:
        return []


def list_children_ids(db: ThreadsDB, parent_id: str) -> list[str]:
    """List all direct child thread IDs for a parent thread.

    Only returns immediate children, not grandchildren or deeper
    descendants. Use ``collect_subtree()`` to get all descendants.

    Args:
        db: ThreadsDB instance for database operations.
        parent_id: ID of the parent thread.

    Returns:
        List of child thread IDs.
    """
    try:
        cur = db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (parent_id,))
        return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def current_open_invoke(db: ThreadsDB, thread_id: str) -> Optional[str]:
    try:
        row = db.current_open(thread_id)
        return row["invoke_id"] if row else None
    except Exception:
        return None


def interrupt_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> Optional[str]:
    """Hard-preempt current step by dropping the current lease.

    Writers that gate on (thread_id, invoke_id) will fail on the next
    heartbeat because the open_streams row for that (thread, invoke)
    no longer exists. A new runner can immediately acquire a fresh
    lease for the thread.
    """
    """Interrupt or cancel the current (or pending) work for a thread.

    Behaviour:
      - If the thread currently has an open stream lease (open_streams row),
        we delete that row so the active runner loses its lease.
        We also append a ``control.interrupt`` event containing the
        ``old_invoke_id`` and the stream ``purpose``.

      - If there is *no* open stream lease, we still want Ctrl+C-like
        interactions to be able to cancel a *pending* RA1 LLM turn
        (a runnable user message that has not yet been picked up by a
        runner). In that case we best-effort infer whether RA1 is
        currently pending and, if so, append a ``control.interrupt``
        boundary event with ``purpose='llm'``.

    The purpose of the boundary event is to advance RA1's
    ``_last_stream_close_seq`` so the same user message does not
    repeatedly re-trigger an LLM call after an interruption.
    """

    cur = db.conn.execute("SELECT invoke_id, purpose FROM open_streams WHERE thread_id=?", (thread_id,))
    row = cur.fetchone()
    old = row[0] if row else None
    purpose = row[1] if row else None
    new_inv = _ulid_like()

    if old:
        # Remove the existing open_streams row so that:
        #  - the current runner loses its lease (heartbeat will fail), and
        #  - future runners can immediately acquire a new lease.
        try:
            db.conn.execute("DELETE FROM open_streams WHERE thread_id=? AND invoke_id=?", (thread_id, old))
        except Exception:
            pass
        db.append_event(
            event_id=_ulid_like(),
            thread_id=thread_id,
            type_='control.interrupt',
            payload={"reason": reason, "old_invoke_id": old, "new_invoke_id": new_inv, "purpose": purpose},
        )
        return old

    # No active lease: best-effort cancel a pending RA1 LLM invocation.
    try:
        from .tool_state import discover_runner_actionable_cached

        ra = discover_runner_actionable_cached(db, thread_id)
        if ra and getattr(ra, 'kind', None) == 'RA1_llm':
            db.append_event(
                event_id=_ulid_like(),
                thread_id=thread_id,
                type_='control.interrupt',
                payload={
                    "reason": reason,
                    "old_invoke_id": None,
                    "new_invoke_id": new_inv,
                    "purpose": "llm",
                    "note": "Cancelled pending RA1 turn (no active open_stream lease)",
                },
            )
    except Exception:
        pass

    return None


def pause_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    """Pause a thread to prevent further execution.

    Sets the thread status to 'paused' and emits a ``control.pause``
    event. The runner will not process paused threads until they
    are resumed with ``resume_thread()``.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread to pause.
        reason: Human-readable reason for pausing (default: 'user').
    """
    db.conn.execute("UPDATE threads SET status='paused' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.pause', payload={"reason": reason})


def resume_thread(db: ThreadsDB, thread_id: str, reason: str = 'user') -> None:
    """Resume a paused thread to allow execution.

    Sets the thread status to 'active' and emits a ``control.resume``
    event. The runner will resume processing the thread if there is
    actionable work pending.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread to resume.
        reason: Human-readable reason for resuming (default: 'user').
    """
    db.conn.execute("UPDATE threads SET status='active' WHERE thread_id=?", (thread_id,))
    db.append_event(event_id=_ulid_like(), thread_id=thread_id, type_='control.resume', payload={"reason": reason})


def set_thread_model(db: ThreadsDB, thread_id: str, model_key: str, reason: str = 'user',
                         concrete_model_info: Optional[Dict[str, Any]] = None,
                         models_path: str = "models.json") -> None:
    """Append a model.switch event to a thread.

    This is the authoritative record of model selection for a thread.
    The ThreadRunner and UIs should not infer the active model from
    message payloads; they should instead call current_thread_model(),
    which uses these events.

    If concrete_model_info is not provided, it will be computed from
    models.json (if eggllm is available). If eggllm is not available,
    the field will be omitted.
    """
    payload = {
        'model_key': model_key,
        'reason': reason,
    }
    if concrete_model_info is None:
        try:
            concrete_model_info = _get_concrete_model_info(model_key, models_path)
        except Exception:
            concrete_model_info = {}
    if concrete_model_info:
        payload['concrete_model_info'] = concrete_model_info
    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='model.switch',
        payload=payload,
    )
def current_thread_model(db: ThreadsDB, thread_id: str) -> Optional[str]:
    """Return the effective model for a thread.

    Precedence:
      1. Most recent model.switch event (by event_seq) in this thread
         whose payload contains a non-empty model_key.
      2. threads.initial_model_key for this thread, if set and non-empty.
      3. None (caller may then fall back to the LLM client's default).

    This helper must be the single source of truth for determining the
    active model for a thread in eggthreads-based applications.
    """
    model_key: Optional[str] = None
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            mk = payload.get('model_key')
            if isinstance(mk, str) and mk.strip():
                model_key = mk.strip()
    except Exception:
        model_key = None

    if not model_key:
        try:
            th = db.get_thread(thread_id)
        except Exception:
            th = None
        imk = getattr(th, 'initial_model_key', None) if th else None
        if isinstance(imk, str) and imk.strip():
            model_key = imk.strip()

    return model_key



def current_thread_model_info(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the concrete_model_info dict from the most recent model.switch event.
    
    Returns None if no model.switch event exists or if the payload lacks
    concrete_model_info.
    """
    import json
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        )
        row = cur.fetchone()
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            return payload.get('concrete_model_info')
    except Exception:
        pass
    return None
def get_thread_working_directory(db: ThreadsDB, thread_id: str) -> Path:
    """Get the effective working directory for a thread.

    Resolves the working directory by checking ``thread.config`` events
    for this thread and its ancestors. If no explicit configuration
    exists, returns the current process working directory.

    Args:
        db: ThreadsDB instance for database operations.
        thread_id: ID of the thread.

    Returns:
        Resolved Path to the thread's working directory.
    """
    from pathlib import Path
    import json
    tid = thread_id
    seen = set()
    cwd = Path.cwd().resolve()
    while tid and tid not in seen:
        seen.add(tid)
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='thread.config' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if row:
            payload = json.loads(row[0])
            wd = payload.get('working_dir')
            if wd:
                return (cwd / wd).resolve()
        # manual parent lookup
        p_row = db.conn.execute("SELECT parent_id FROM children WHERE child_id=?", (tid,)).fetchone()
        tid = p_row[0] if p_row else None
    return cwd

def set_thread_working_directory(db: ThreadsDB, thread_id: str, working_dir: str, reason: str = "user") -> None:
    """Set the working directory for a thread.
    
    The directory must be a subdirectory of the current process working directory.
    It cannot be inside the .egg folder.
    """
    import os
    from pathlib import Path

    cwd = Path.cwd().resolve()
    target = Path(working_dir).resolve()
    
    if not str(target).startswith(str(cwd)):
         raise ValueError(f"Working directory {working_dir} must be a subdirectory of {cwd}")

    if ".egg" in target.parts:
         raise ValueError("Working directory cannot be inside the .egg system folder")

    target.mkdir(parents=True, exist_ok=True)
    rel_path = os.path.relpath(target, cwd)

    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_='thread.config',
        payload={
            'working_dir': rel_path,
            'reason': reason
        }
    )

def _ensure_thread_working_directory(db: ThreadsDB, thread_id: str) -> Path:
    """Resolve and physically create the working directory for a thread if it is missing."""
    wd = get_thread_working_directory(db, thread_id)
    if wd and not wd.exists():
        wd.mkdir(parents=True, exist_ok=True)
    return wd

def collect_subtree(db: ThreadsDB, root_id: str) -> list[str]:
    """Return all thread_ids in the subtree rooted at ``root_id`` (BFS)."""
    out: list[str] = []
    q: list[str] = [root_id]
    seen = set()
    while q:
        t = q.pop(0)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        try:
            cur = db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (t,))
            for row in cur.fetchall():
                q.append(row[0])
        except Exception:
            continue
    return out


def list_active_threads(db: ThreadsDB, subtree: list[str]) -> list[str]:
    """Return list of thread_ids that are currently running or runnable."""
    from datetime import datetime
    active: list[str] = []
    for tid in subtree:
        is_running = False
        try:
            row_open = db.current_open(tid)
            if row_open:
                # Only consider thread running if lease hasn't expired
                lease_until = row_open['lease_until']
                now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                if lease_until and lease_until > now_iso:
                    is_running = True
        except Exception:
            pass
        if is_running or is_thread_runnable(db, tid):
            active.append(tid)
    return active


async def wait_subtree_idle(db: ThreadsDB, root_id: str, poll_sec: float = 0.1, quiet_checks: int = 3) -> None:
    """Wait until no threads in the subtree are running or runnable for N checks."""
    import asyncio
    subtree = collect_subtree(db, root_id)
    stable = 0
    while True:
        if not list_active_threads(db, subtree):
            stable += 1
            if stable >= quiet_checks:
                return
        else:
            stable = 0
        await asyncio.sleep(poll_sec)
        # Refresh subtree in case new children were spawned
        subtree = collect_subtree(db, root_id)

def word_count_from_snapshot(db: ThreadsDB, thread_id: str) -> int:
    """Return the word count of all messages in the thread snapshot."""
    row = db.get_thread(thread_id)
    if not row or not row.snapshot_json:
        return 0
    try:
        msgs = json.loads(row.snapshot_json).get("messages", [])
        return sum(len(str(m.get("content") or "").split()) for m in msgs)
    except Exception:
        return 0


def word_count_from_events(db: ThreadsDB, thread_id: str) -> int:
    """Return word count of thread, including events after last snapshot."""
    base = word_count_from_snapshot(db, thread_id)
    row = db.get_thread(thread_id)
    last_seq = int(row.snapshot_last_event_seq) if row else -1
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND event_seq>?",
        (thread_id, last_seq),
    )
    extra = 0
    for (pj,) in cur.fetchall():
        try:
            p = json.loads(pj) if isinstance(pj, str) else (pj or {})
            # Content
            c = p.get("content")
            if isinstance(c, str):
                extra += len(c.split())
            # Reasoning (msg.create)
            r = p.get("reasoning")
            if isinstance(r, str):
                extra += len(r.split())
            # Stream deltas (text or reason)
            t = p.get("text") or p.get("reason")
            if isinstance(t, str):
                extra += len(t.split())
            # Tool args
            tc = p.get("tool_call")
            if isinstance(tc, dict):
                a = tc.get("arguments_delta")
                if isinstance(a, str):
                    extra += len(a.split())
        except Exception:
            pass
    return base + extra


def approve_tool_calls_for_thread(db, thread_id, decision='all-in-turn', reason=None, tool_call_id=None):
    """Approve tool calls for a thread with a given decision.

    Creates a tool_call.approval event that can be used by the runner to
    automatically approve tool calls according to the decision.

    Args:
        db: ThreadsDB instance
        thread_id: target thread
        decision: one of 'all-in-turn', 'granted', 'denied', 'global_approval',
                  'revoke_global_approval', 'prompt'
        reason: optional human-readable reason for the decision
        tool_call_id: optional specific tool call ID to approve/deny.
                      If omitted, the decision applies to the whole thread
                      (or to the current turn, depending on the decision).
    """
    import os
    payload = {'decision': decision}
    if reason is not None:
        payload['reason'] = reason
    if tool_call_id is not None:
        payload['tool_call_id'] = tool_call_id
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_='tool_call.approval',
        msg_id=None,
        invoke_id=None,
        payload=payload,
    )

def execute_bash_command(db: ThreadsDB, thread_id: str, script: str, hidden: bool = False) -> str:
    """Execute a bash command as a user tool call (RA3).

    This mimics the UI's $ (visible) and $$ (hidden) commands. It appends a
    user message with a tool_call for the 'bash' tool, automatically approves
    it, and returns the tool_call_id.

    Args:
        db: ThreadsDB instance.
        thread_id: The thread where the command should be executed.
        script: Bash script to run.
        hidden: If True, the command is marked no_api and its output will not
                be shown to the LLM (corresponds to '$$').

    Returns:
        The tool_call_id of the created tool call, which can be used to later
        retrieve the result via get_user_command_result.
    """
    import os
    tool_call_id = _ulid_like()
    tool_call = {
        'id': tool_call_id,
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
    prefix = '$$ ' if hidden else '$ '
    append_message(db, thread_id, 'user', f"{prefix}{script}", extra=extra)
    approve_tool_calls_for_thread(db, thread_id, decision='granted',
                                  reason='Auto-approved as user-initiated bash command',
                                  tool_call_id=tool_call_id)
    return tool_call_id


def execute_bash_command_hidden(db: ThreadsDB, thread_id: str, script: str) -> str:
    """Convenience wrapper for execute_bash_command with hidden=True."""
    return execute_bash_command(db, thread_id, script, hidden=True)


def get_user_command_result(db: ThreadsDB, thread_id: str, tool_call_id: str) -> Optional[str]:
    """Retrieve the tool message content for a user command tool call.

    Returns the content of the tool message that corresponds to the given
    tool_call_id, if such a message has been published (state TC6). If the
    tool call is not yet published, returns None.

    Args:
        db: ThreadsDB instance.
        thread_id: The thread containing the tool call.
        tool_call_id: The tool call ID returned by execute_bash_command.

    Returns:
        The content string of the tool message, or None if not yet published.
    """
    import json as _json
    cur = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC",
        (thread_id,),
    )
    for (pj,) in cur.fetchall():
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            continue
        if payload.get('role') == 'tool' and payload.get('tool_call_id') == tool_call_id:
            return payload.get('content')
    return None


def wait_for_user_command_result(db: ThreadsDB, thread_id: str, tool_call_id: str,
                                 timeout_sec: float = 30.0, poll_interval: float = 0.1) -> Optional[str]:
    """Wait for a user command tool call to finish and return its result.

    Polls the thread's tool call state until the tool call is published (TC6)
    or the timeout expires. Returns the tool message content if published,
    otherwise None.

    Args:
        db: ThreadsDB instance.
        thread_id: The thread containing the tool call.
        tool_call_id: The tool call ID returned by execute_bash_command.
        timeout_sec: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Returns:
        The content string of the tool message, or None on timeout.
    """
    import time
    from .tool_state import build_tool_call_states
    start = time.time()
    while time.time() - start < timeout_sec:
        states = build_tool_call_states(db, thread_id)
        tc = states.get(tool_call_id)
        if tc is not None and tc.published:
            return get_user_command_result(db, thread_id, tool_call_id)
        time.sleep(poll_interval)
    return None

async def wait_for_user_command_result_async(db: ThreadsDB, thread_id: str, tool_call_id: str,
                                             timeout_sec: float = 30.0, poll_interval: float = 0.1) -> Optional[str]:
    """Async version of wait_for_user_command_result."""
    import asyncio
    from .tool_state import build_tool_call_states
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < timeout_sec:
        states = build_tool_call_states(db, thread_id)
        tc = states.get(tool_call_id)
        if tc is not None and tc.published:
            return get_user_command_result(db, thread_id, tool_call_id)
        await asyncio.sleep(poll_interval)
    return None


async def execute_bash_command_async(db: ThreadsDB, thread_id: str, script: str, hidden: bool = False,
                                     timeout_sec: float = 30.0, poll_interval: float = 0.1) -> Optional[str]:
    """Execute a bash command as a user tool call and wait for its result asynchronously.

    Returns the tool message content if the tool call finishes within timeout_sec,
    otherwise None.
    """
    tool_call_id = execute_bash_command(db, thread_id, script, hidden=hidden)
    return await wait_for_user_command_result_async(db, thread_id, tool_call_id,
                                                    timeout_sec=timeout_sec,
                                                    poll_interval=poll_interval)

def set_subtree_working_directory(db: ThreadsDB, root_thread_id: str, working_dir: str, reason: str = "user") -> None:
    """Apply working directory configuration to all threads in a subtree."""
    for tid in collect_subtree(db, root_thread_id):
        set_thread_working_directory(db, tid, working_dir, reason=reason)
