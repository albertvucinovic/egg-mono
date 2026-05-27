from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from .db import ThreadsDB, ThreadRow
from .snapshot import SnapshotBuilder

try:
    from eggllm.config import load_models_config
    from eggllm.registry import ModelRegistry
    from eggllm.catalog import AllModelsCatalog
    EGGLLM_AVAILABLE = True
except ImportError:
    EGGLLM_AVAILABLE = False


def _get_default_model_key(models_path: str = "models.json") -> Optional[str]:
    """Return the default_model key from models.json, or None if unavailable."""
    env_model = (os.environ.get("EGG_STARTING_MODEL") or "").strip()
    if env_model:
        return env_model
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


def _utcnow_iso() -> str:
    """SQLite-compatible current UTC timestamp string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def validate_model_handle(model_handle: str, models_path: str = "models.json",
                          all_models_path: str | None = None) -> bool:
    """Check if a model handle exists in models.json or all-models.json catalog.

    Supports both named models from models.json and catalog models using the
    ``all:provider:model`` format (populated by ``/updateAllModels``).

    Args:
        model_handle: The model name/key to validate
        models_path: Path to models.json file
        all_models_path: Path to all-models.json catalog file (optional)

    Returns:
        True if the model handle exists, False otherwise
    """
    import os.path
    if not model_handle or not model_handle.strip():
        return False
    model_handle = model_handle.strip()

    if EGGLLM_AVAILABLE:
        try:
            models_config, providers_config = load_models_config(models_path)
            catalog = AllModelsCatalog(all_models_path)
            registry = ModelRegistry(models_config, providers_config, catalog)
            resolved = registry.resolve(model_handle)
            return resolved is not None
        except Exception:
            pass

    # Fallback: parse models.json directly
    if not os.path.exists(models_path):
        return False
    try:
        with open(models_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        providers = data.get('providers', {})
        for provider_data in providers.values():
            models = provider_data.get('models', {})
            if model_handle in models:
                return True
        # Fallback for all: prefix - if it starts with all:provider:model, accept it
        # as long as the provider exists in models.json
        if model_handle.startswith('all:'):
            rest = model_handle[4:]
            if ':' in rest:
                prov = rest.split(':', 1)[0]
                if prov in providers:
                    return True
        return False
    except Exception:
        return False


def _get_concrete_model_info(model_key: str, models_path: str = "models.json",
                             all_models_path: str | None = None):
    """Return nested providers dict for a given model key.

    Supports both named models from models.json and catalog models using the
    ``all:provider:model`` format.

    Raises ValueError if model_key not found or eggllm not available.
    """
    # First try eggllm if available
    try:
        from eggllm.config import load_models_config
        from eggllm.registry import ModelRegistry
        from eggllm.catalog import AllModelsCatalog
    except ImportError:
        # eggllm not available, fall back to direct parsing
        load_models_config = None

    if load_models_config is not None:
        try:
            models_config, providers_config = load_models_config(models_path)
            catalog = AllModelsCatalog(all_models_path)
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
                       models_path: str = "models.json", all_models_path: str | None = None) -> str:
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
        all_models_path: Path to all-models.json catalog file (optional).

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
        set_thread_model(db, tid, effective_model_key, reason='initial', models_path=models_path,
                         all_models_path=all_models_path)
    return tid


def create_child_thread(db: ThreadsDB, parent_id: str, name: Optional[str] = None, initial_model_key: Optional[str] = None,
                        models_path: str = "models.json", all_models_path: str | None = None,
                        inherit_tools_config: bool = True) -> str:
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
        all_models_path: Path to all-models.json catalog file (optional).
        inherit_tools_config: When True (default), copy the parent's
            current effective tools configuration onto the child at creation
            time. Trusted programmatic callers may set this False or widen the
            child afterwards with the tools configuration helpers.

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
        set_thread_model(db, tid, initial_model_key, reason='initial', models_path=models_path,
                         all_models_path=all_models_path)
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

    # Tool capability config is intentionally copied by value at creation time
    # (like model config) rather than resolved dynamically through ancestors.
    # This gives new children the parent's current restrictions without making
    # later parent changes silently mutate existing children. Programmatic code
    # with DB/API access can still widen the child explicitly after creation.
    if inherit_tools_config:
        try:
            from .tools_config import inherit_tools_config_for_child
            inherit_tools_config_for_child(db, parent_id, tid)
        except Exception:
            # Best-effort: thread creation should not fail solely because an
            # advisory tools.config event could not be copied.
            pass

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

    The duplicate also preserves the source thread's effective configuration:
    - **Working directory**: Copies the ``thread.config`` event (inherited
      or explicit) so the duplicate uses the same working directory.
    - **Sandbox settings**: Copies the ``sandbox.config`` event (inherited
      or explicit) so the duplicate runs in the same sandbox environment.
    - **Active model**: Copies the ``model.switch`` event (inherited or
      explicit) so the duplicate uses the same LLM model.

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

    # Copy working directory configuration from the source thread or its
    # ancestors. Since the duplicate is a root thread (no parent), it won't
    # inherit settings, so we must copy the effective config explicitly.
    wd_payload = _nearest_working_dir_payload(db, source_thread_id)
    if wd_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='thread.config',
            payload=wd_payload,
        )

    # Copy sandbox configuration from the source thread or its ancestors.
    # This preserves the effective sandbox settings (enabled state, provider,
    # settings) so the duplicate runs in the same sandbox environment.
    from .sandbox import _nearest_sandbox_event_payload
    sandbox_payload = _nearest_sandbox_event_payload(db, source_thread_id)
    if sandbox_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='sandbox.config',
            payload=sandbox_payload,
        )

    # Copy the active model configuration from the source thread or its
    # ancestors. This preserves model.switch events so the duplicate uses
    # the same model (including any concrete_model_info for ephemeral models).
    model_payload = _nearest_model_switch_payload(db, source_thread_id)
    if model_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='model.switch',
            payload=model_payload,
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

    The duplicate also preserves the source thread's effective configuration
    (working directory, sandbox settings, and active model), same as duplicate_thread.

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

    # Copy working directory configuration from the source thread or its
    # ancestors. Since the duplicate is a root thread (no parent), it won't
    # inherit settings, so we must copy the effective config explicitly.
    wd_payload = _nearest_working_dir_payload(db, source_thread_id)
    if wd_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='thread.config',
            payload=wd_payload,
        )

    # Copy sandbox configuration from the source thread or its ancestors.
    # This preserves the effective sandbox settings (enabled state, provider,
    # settings) so the duplicate runs in the same sandbox environment.
    from .sandbox import _nearest_sandbox_event_payload
    sandbox_payload = _nearest_sandbox_event_payload(db, source_thread_id)
    if sandbox_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='sandbox.config',
            payload=sandbox_payload,
        )

    # Copy the active model configuration from the source thread or its
    # ancestors. This preserves model.switch events so the duplicate uses
    # the same model (including any concrete_model_info for ephemeral models).
    model_payload = _nearest_model_switch_payload(db, source_thread_id)
    if model_payload:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=new_tid,
            type_='model.switch',
            payload=model_payload,
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


THREAD_RECOVERY_EVENT_TYPE = 'thread.recovery'


@dataclass
class ThreadRecoverySettings:
    """Effective recovery settings for a thread."""

    auto_continue_on_error: bool = True


RECOVERY_NOTICE_EXTRA: Dict[str, bool] = {
    'no_api': True,
    'recovery_notice': True,
    'preserve_on_continue': True,
}


def _get_event_seq_for_msg_id(db: ThreadsDB, thread_id: str, msg_id: str) -> Optional[int]:
    """Get the event_seq for a message ID."""
    cur = db.conn.execute(
        "SELECT event_seq FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create' ORDER BY event_seq ASC LIMIT 1",
        (thread_id, msg_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_thread_recovery(db: ThreadsDB, thread_id: str) -> ThreadRecoverySettings:
    """Return effective recovery settings, inherited from nearest ancestor."""

    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq DESC LIMIT 1",
                (tid, THREAD_RECOVERY_EVENT_TYPE),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and isinstance(payload.get('autoContinueOnError'), bool):
                return ThreadRecoverySettings(auto_continue_on_error=payload['autoContinueOnError'])
        try:
            parent = db.conn.execute(
                "SELECT parent_id FROM children WHERE child_id=? LIMIT 1",
                (tid,),
            ).fetchone()
            tid = parent[0] if parent and parent[0] else None
        except Exception:
            tid = None
    return ThreadRecoverySettings()


def set_thread_recovery(
    db: ThreadsDB,
    thread_id: str,
    *,
    auto_continue_on_error: bool,
) -> None:
    """Persist recovery settings for a thread."""

    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_=THREAD_RECOVERY_EVENT_TYPE,
        payload={'autoContinueOnError': bool(auto_continue_on_error)},
    )


def append_recovery_notice(
    db: ThreadsDB,
    thread_id: str,
    content: str,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Append a persistent local recovery/status notice to a thread."""

    payload_extra: Dict[str, Any] = dict(extra or {})
    payload_extra.update(RECOVERY_NOTICE_EXTRA)
    msg_id = append_message(db, thread_id, 'system', content, extra=payload_extra)
    create_snapshot(db, thread_id)
    return msg_id


def _payload_is_recovery_notice(payload: Dict[str, Any]) -> bool:
    return bool(payload.get('recovery_notice'))


def _provider_error_text_from_payload(payload: Dict[str, Any]) -> str:
    if _payload_is_recovery_notice(payload):
        return ''
    content = payload.get('content')
    if not isinstance(content, str):
        return ''
    low = content.lower()
    if (
        'llm/runner error' in low
        or 'llm error' in low
        or 'context limit exceeded' in low
        or low.startswith('error:')
    ):
        return content.strip()
    return ''


def _continue_notice_skipped_payloads(
    db: ThreadsDB,
    thread_id: str,
    skipped_msg_ids: List[str],
) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for msg_id in skipped_msg_ids:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND msg_id=? AND type='msg.create' ORDER BY event_seq ASC LIMIT 1",
            (thread_id, msg_id),
        )
        row = cur.fetchone()
        if not row:
            continue
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _format_continue_recovery_notice(db: ThreadsDB, thread_id: str, result: ContinueResult, *, source: str) -> str:
    skipped_payloads = _continue_notice_skipped_payloads(db, thread_id, result.skipped_msg_ids)

    role_counts: Dict[str, int] = {}
    provider_error = ''
    incomplete_info = ''
    for payload in skipped_payloads:
        role = payload.get('role')
        role_name = role if isinstance(role, str) and role else 'unknown'
        role_counts[role_name] = role_counts.get(role_name, 0) + 1

        error_text = _provider_error_text_from_payload(payload)
        if error_text:
            provider_error = error_text
        incomplete_reason = payload.get('incomplete_reason')
        if payload.get('incomplete') or incomplete_reason:
            incomplete_info = str(incomplete_reason or 'assistant message marked incomplete')

    skipped_count = len(result.skipped_msg_ids)
    skipped_word = 'message' if skipped_count == 1 else 'messages'
    role_summary = ', '.join(f"{role}={count}" for role, count in sorted(role_counts.items()))
    skipped_line = f"Skipped: {skipped_count} {skipped_word}"
    if role_summary:
        skipped_line += f" ({role_summary})"
    skipped_line += '.'

    lines = [f"Recovery: {source} succeeded."]
    if result.continue_from_msg_id:
        msg_id = result.continue_from_msg_id
        lines.append(f"Continue point: {msg_id[-8:]} ({msg_id}).")
    else:
        lines.append("Continue point: none selected.")
    lines.append(skipped_line)
    if provider_error:
        lines.append(f"Previous error: {_truncate_status_text(provider_error, limit=300)}")
    if incomplete_info:
        lines.append(f"Previous incomplete: {_truncate_status_text(incomplete_info, limit=300)}")
    if result.diagnosis and result.diagnosis.issues:
        lines.append(f"Diagnosis: {'; '.join(result.diagnosis.issues)}")
    return '\n'.join(lines)


def append_continue_recovery_notice(
    db: ThreadsDB,
    thread_id: str,
    result: ContinueResult,
    *,
    source: str = 'manual /continue',
) -> str:
    """Append a local recovery notice describing a successful continue."""

    return append_recovery_notice(
        db,
        thread_id,
        _format_continue_recovery_notice(db, thread_id, result, source=source),
    )


# --------- Compaction API -----------------------------------------------------

COMPACTION_EVENT_TYPE = 'thread.compaction'
COMPACTION_CONTEXT_LENGTH_EVENT_TYPE = 'thread.compaction_context_length'
COMPACTION_SUMMARY_IN_PROGRESS_EVENT_TYPE = 'thread.compaction_summary_in_progress'

COMPACTION_CHECKPOINT_SKILL_NAME = 'compaction-checkpoint'

COMPACTION_SUMMARY_REQUEST = (
    f"Use the `{COMPACTION_CHECKPOINT_SKILL_NAME}` skill. Mode: `summary_only`."
)
AUTO_COMPACTION_SUMMARY_REQUEST = (
    f"Use the `{COMPACTION_CHECKPOINT_SKILL_NAME}` skill. Mode: `checkpoint_and_resume`."
)

_FALSE_LIKE_ENV_VALUES = {'0', 'false', 'no', 'off'}


@dataclass
class CompactionStartResolution:
    """Resolved provider-context start message for a compaction request."""

    success: bool
    selector: str
    msg_id: Optional[str]
    event_seq: Optional[int]
    role: Optional[str]
    message: str


@dataclass
class CompactionCommitResult:
    """Result of committing a ``thread.compaction`` boundary event."""

    success: bool
    selector: str
    start_msg_id: Optional[str]
    start_event_seq: Optional[int]
    compaction_event_seq: Optional[int]
    message: str


@dataclass
class CompactionSummaryRequestResult:
    """Result of committing compaction and appending a summary request."""

    success: bool
    request_msg_id: Optional[str]
    marker_event_seq: Optional[int]
    message: str
    compaction: Optional[CompactionCommitResult] = None


def _compaction_summary_request_extra(*, created_by: str) -> Dict[str, Any]:
    return {
        'created_by': created_by,
        'compaction_summary_request': True,
    }


def _normal_user_messages_after_seq(db: ThreadsDB, thread_id: str, after_seq: int) -> List[Dict[str, Any]]:
    skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    cur = db.conn.execute(
        "SELECT event_seq, msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.create' AND event_seq>? ORDER BY event_seq ASC",
        (thread_id, int(after_seq)),
    )
    out: List[Dict[str, Any]] = []
    for event_seq, msg_id, payload_json in cur.fetchall():
        if msg_id and (str(msg_id) in skipped or str(msg_id) in deleted):
            continue
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or payload.get('role') != 'user':
            continue
        if payload.get('no_api') or payload.get('keep_user_turn') or payload.get('tool_calls'):
            continue
        if payload.get('compaction_summary_request') or payload.get('auto_compaction_request'):
            continue
        out.append({'event_seq': int(event_seq), 'msg_id': msg_id, 'payload': payload})
    return out


def _has_unhandled_user_message_after_seq(db: ThreadsDB, thread_id: str, after_seq: int) -> bool:
    """Return True when a normal provider-visible user message exists after ``after_seq``."""

    return bool(_normal_user_messages_after_seq(db, thread_id, after_seq))


@dataclass
class AutoCompactionResult:
    """Result of checking and possibly committing automatic compaction."""

    triggered: bool
    attempted: bool
    context_tokens: int
    threshold_tokens: Optional[int]
    compaction: Optional[CompactionCommitResult]
    message: str


@dataclass
class AutoCompactionThresholdResolution:
    """Resolved auto-compaction token threshold and source."""

    enabled: bool
    threshold_tokens: Optional[int]
    source: str
    message: str


def _compaction_skipped_and_deleted_msg_ids(db: ThreadsDB, thread_id: str) -> tuple[set[str], set[str]]:
    skipped: set[str] = set()
    deleted: set[str] = set()
    cur = db.conn.execute(
        "SELECT type, msg_id, payload_json FROM events WHERE thread_id=? AND type IN ('msg.edit', 'msg.delete')",
        (thread_id,),
    )
    for type_, msg_id, payload_json in cur.fetchall():
        if not msg_id:
            continue
        if type_ == 'msg.delete':
            deleted.add(str(msg_id))
            continue
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get('skipped_on_continue'):
            skipped.add(str(msg_id))
    return skipped, deleted


def list_thread_compactions(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    """Return raw ``thread.compaction`` events for a thread in event order."""

    cur = db.conn.execute(
        "SELECT event_seq, event_id, ts, payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
        (thread_id, COMPACTION_EVENT_TYPE),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        try:
            payload = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else (row['payload_json'] or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        item = dict(payload)
        item['event_seq'] = int(row['event_seq'])
        item['event_id'] = row['event_id']
        item['ts'] = row['ts']
        out.append(item)
    return out


def latest_thread_compaction(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest raw ``thread.compaction`` event for a thread."""

    compactions = list_thread_compactions(db, thread_id)
    return compactions[-1] if compactions else None


def set_thread_compaction_context_length(
    db: ThreadsDB,
    thread_id: str,
    threshold_tokens: int,
    *,
    created_by: str = 'user',
) -> int:
    """Append a thread-level auto-compaction context threshold override.

    The latest effective ``thread.compaction_context_length`` event wins when
    resolving automatic compaction thresholds.  Positive values enable
    auto-compaction at that provider-context token estimate; zero or negative
    values disable auto-compaction for this thread until superseded by a later
    effective override event.
    """

    threshold_int = int(threshold_tokens)
    return int(db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_=COMPACTION_CONTEXT_LENGTH_EVENT_TYPE,
        payload={
            'threshold_tokens': threshold_int,
            'created_by': created_by,
            'created_at': _utcnow_iso(),
        },
    ))


def list_thread_compaction_context_lengths(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    """Return raw ``thread.compaction_context_length`` events in event order."""

    cur = db.conn.execute(
        "SELECT event_seq, event_id, ts, payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
        (thread_id, COMPACTION_CONTEXT_LENGTH_EVENT_TYPE),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        try:
            payload = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else (row['payload_json'] or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        item = dict(payload)
        item['event_seq'] = int(row['event_seq'])
        item['event_id'] = row['event_id']
        item['ts'] = row['ts']
        out.append(item)
    return out


def append_compaction_summary_in_progress(
    db: ThreadsDB,
    thread_id: str,
    *,
    request_msg_id: Optional[str] = None,
    created_by: str = 'auto_compaction',
) -> int:
    """Append a marker for an automatic summary compaction request.

    The marker is a control event, not provider-visible content.  It prevents
    repeated threshold checks from appending duplicate summary requests while
    the assistant has not yet satisfied the post-compaction request by
    emitting a later assistant response.
    """

    payload: Dict[str, Any] = {
        'created_by': created_by,
        'created_at': _utcnow_iso(),
    }
    if request_msg_id:
        payload['request_msg_id'] = request_msg_id
    return int(db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_=COMPACTION_SUMMARY_IN_PROGRESS_EVENT_TYPE,
        payload=payload,
    ))


def list_thread_compaction_summary_in_progress_events(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    """Return raw summary-in-progress markers in event order."""

    cur = db.conn.execute(
        "SELECT event_seq, event_id, ts, payload_json FROM events WHERE thread_id=? AND type=? ORDER BY event_seq ASC",
        (thread_id, COMPACTION_SUMMARY_IN_PROGRESS_EVENT_TYPE),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        try:
            payload = json.loads(row['payload_json']) if isinstance(row['payload_json'], str) else (row['payload_json'] or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        item = dict(payload)
        item['event_seq'] = int(row['event_seq'])
        item['event_id'] = row['event_id']
        item['ts'] = row['ts']
        out.append(item)
    return out


def _continue_erased_event_ranges(db: ThreadsDB, thread_id: str) -> List[tuple[int, int]]:
    """Return raw event ranges made ineffective by later ``/continue`` calls.

    ``continue_thread`` preserves the raw event log but marks message creates
    after the continue point as skipped.  Non-message control events (including
    ``thread.compaction``) need the same effective-view treatment for provider
    context: a later continue from message P erases control events in
    ``(P.event_seq, continue_event_seq)``.
    """

    cur = db.conn.execute(
        "SELECT event_seq, payload_json FROM events WHERE thread_id=? AND type='control.interrupt' ORDER BY event_seq ASC",
        (thread_id,),
    )
    ranges: List[tuple[int, int]] = []
    for event_seq, payload_json in cur.fetchall():
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or payload.get('purpose') != 'continue':
            continue
        msg_id = payload.get('continue_from_msg_id')
        if not isinstance(msg_id, str) or not msg_id:
            continue
        continue_from_seq = _get_event_seq_for_msg_id(db, thread_id, msg_id)
        if continue_from_seq is None:
            continue
        ranges.append((int(continue_from_seq), int(event_seq)))
    return ranges


def _event_erased_by_continue(event_seq: int, erased_ranges: List[tuple[int, int]]) -> bool:
    return any(start_seq < event_seq < continue_seq for start_seq, continue_seq in erased_ranges)


def latest_effective_thread_compaction_context_length(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest context-length override still active in the effective view."""

    erased_ranges = _continue_erased_event_ranges(db, thread_id)
    for event in reversed(list_thread_compaction_context_lengths(db, thread_id)):
        try:
            event_seq = int(event.get('event_seq'))
        except Exception:
            continue
        if _event_erased_by_continue(event_seq, erased_ranges):
            continue
        return event
    return None


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and not value.strip():
            return None
        return int(value)
    except Exception:
        return None


def _positive_int_or_none(value: Any) -> Optional[int]:
    parsed = _int_or_none(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _model_context_window_from_concrete_info(
    concrete_model_info: Any,
    *,
    model_key: Optional[str] = None,
) -> Optional[int]:
    """Extract a positive context-window ``max_tokens`` from concrete model info."""

    if not isinstance(concrete_model_info, Mapping):
        return None

    # Be tolerant of simplified concrete info shapes in tests or callers.
    direct = _positive_int_or_none(concrete_model_info.get('max_tokens'))
    if direct is not None:
        return direct

    providers = concrete_model_info.get('providers')
    if not isinstance(providers, Mapping):
        return None

    def _model_maps() -> List[Mapping[str, Any]]:
        out: List[Mapping[str, Any]] = []
        for provider_cfg in providers.values():
            if not isinstance(provider_cfg, Mapping):
                continue
            models = provider_cfg.get('models')
            if isinstance(models, Mapping):
                out.append(models)
        return out

    if model_key:
        for models in _model_maps():
            cfg = models.get(model_key)
            if isinstance(cfg, Mapping):
                value = _positive_int_or_none(cfg.get('max_tokens'))
                if value is not None:
                    return value

    for models in _model_maps():
        for cfg in models.values():
            if not isinstance(cfg, Mapping):
                continue
            value = _positive_int_or_none(cfg.get('max_tokens'))
            if value is not None:
                return value
    return None


def current_thread_model_context_window_tokens(
    db: ThreadsDB,
    thread_id: str,
    *,
    models_path: str = "models.json",
    all_models_path: str | None = None,
) -> Optional[int]:
    """Return the current model context-window length from concrete model metadata."""

    model_key = current_thread_model(db, thread_id)
    concrete = current_thread_model_info(db, thread_id)
    value = _model_context_window_from_concrete_info(concrete, model_key=model_key)
    if value is not None:
        return value
    if model_key:
        try:
            concrete = _get_concrete_model_info(model_key, models_path, all_models_path=all_models_path)
            return _model_context_window_from_concrete_info(concrete, model_key=model_key)
        except Exception:
            return None
    return None


def resolve_auto_compact_threshold(
    db: ThreadsDB,
    thread_id: str,
    explicit_threshold_tokens: Optional[int] = None,
    *,
    models_path: str = "models.json",
    all_models_path: str | None = None,
    environ: Optional[Mapping[str, str]] = None,
) -> AutoCompactionThresholdResolution:
    """Resolve the effective automatic compaction threshold for a thread.

    Precedence is:

    1. latest effective ``thread.compaction_context_length`` event;
    2. explicit ``RunnerConfig.auto_compact_threshold_tokens``;
    3. 80% of the current model ``max_tokens`` context window;
    4. ``EGG_AUTO_COMPACT_THRESHOLD_TOKENS``;
    5. fallback default ``150000``.

    Non-positive thread, explicit runner, or env values disable
    auto-compaction at that source rather than falling through to lower
    precedence sources.
    """

    override = latest_effective_thread_compaction_context_length(db, thread_id)
    if override is not None:
        threshold = _int_or_none(override.get('threshold_tokens'))
        if threshold is None or threshold <= 0:
            return AutoCompactionThresholdResolution(False, None, 'thread_event', 'Auto compaction disabled by thread override.')
        return AutoCompactionThresholdResolution(True, threshold, 'thread_event', 'Auto compaction threshold set by thread override.')

    if explicit_threshold_tokens is not None:
        threshold = _int_or_none(explicit_threshold_tokens)
        if threshold is None or threshold <= 0:
            return AutoCompactionThresholdResolution(False, None, 'runner_config', 'Auto compaction disabled by runner config.')
        return AutoCompactionThresholdResolution(True, threshold, 'runner_config', 'Auto compaction threshold set by runner config.')

    model_max = current_thread_model_context_window_tokens(
        db,
        thread_id,
        models_path=models_path,
        all_models_path=all_models_path,
    )
    if model_max is not None:
        threshold = max(1, int(model_max * 0.8))
        return AutoCompactionThresholdResolution(True, threshold, 'model_max_tokens', 'Auto compaction threshold derived from current model max_tokens.')

    env = os.environ if environ is None else environ
    raw_env = env.get('EGG_AUTO_COMPACT_THRESHOLD_TOKENS') if env is not None else None
    if raw_env is not None and str(raw_env).strip():
        threshold = _int_or_none(raw_env)
        if threshold is None:
            threshold = 150000
            return AutoCompactionThresholdResolution(True, threshold, 'default', 'Auto compaction threshold using fallback default.')
        if threshold <= 0:
            return AutoCompactionThresholdResolution(False, None, 'env', 'Auto compaction disabled by EGG_AUTO_COMPACT_THRESHOLD_TOKENS.')
        return AutoCompactionThresholdResolution(True, threshold, 'env', 'Auto compaction threshold set by EGG_AUTO_COMPACT_THRESHOLD_TOKENS.')

    return AutoCompactionThresholdResolution(True, 150000, 'default', 'Auto compaction threshold using fallback default.')


def auto_compact_summary_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Return whether automatic compaction should request a summary.

    ``EGG_COMPACT_SUMMARY`` defaults to enabled.  Only explicit false-like
    values (``0``, ``false``, ``no``, ``off``) select the direct compaction
    policy.
    """

    env = os.environ if environ is None else environ
    raw = env.get('EGG_COMPACT_SUMMARY') if env is not None else None
    if raw is None:
        return True
    text = str(raw).strip().lower()
    if not text:
        return True
    return text not in _FALSE_LIKE_ENV_VALUES


def latest_effective_thread_compaction(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest ``thread.compaction`` still active in the effective view."""

    erased_ranges = _continue_erased_event_ranges(db, thread_id)
    skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    for compaction in reversed(list_thread_compactions(db, thread_id)):
        try:
            compaction_seq = int(compaction.get('event_seq'))
        except Exception:
            continue
        if _event_erased_by_continue(compaction_seq, erased_ranges):
            continue
        start_msg_id = compaction.get('start_msg_id')
        if isinstance(start_msg_id, str) and (start_msg_id in skipped or start_msg_id in deleted):
            continue
        return compaction
    return None


def latest_effective_thread_compaction_summary_in_progress(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest pending automatic summary marker, if any.

    A marker is pending only when it is still present in the effective thread
    view, appears after the current effective compaction start, and its
    request has not received a later effective assistant response.
    """

    erased_ranges = _continue_erased_event_ranges(db, thread_id)
    skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    current = latest_effective_thread_compaction(db, thread_id)
    current_start_seq = -1
    current_compaction_event_seq = -1
    if current is not None:
        try:
            current_start_seq = int(current.get('start_event_seq'))
        except Exception:
            current_start_seq = -1
        try:
            current_compaction_event_seq = int(current.get('event_seq'))
        except Exception:
            current_compaction_event_seq = -1

    def _has_later_assistant_response(request_seq: int) -> bool:
        for event_seq, _msg_id, payload in _compaction_candidate_messages(db, thread_id):
            if event_seq <= request_seq:
                continue
            if _event_erased_by_continue(event_seq, erased_ranges):
                continue
            if payload.get('no_api'):
                continue
            if payload.get('role') == 'assistant':
                return True
        return False

    for marker in reversed(list_thread_compaction_summary_in_progress_events(db, thread_id)):
        try:
            marker_seq = int(marker.get('event_seq'))
        except Exception:
            continue
        if _event_erased_by_continue(marker_seq, erased_ranges):
            continue
        request_msg_id = marker.get('request_msg_id')
        request_seq: Optional[int] = None
        if isinstance(request_msg_id, str):
            if request_msg_id in skipped or request_msg_id in deleted:
                continue
            if request_msg_id:
                request_seq = _get_event_seq_for_msg_id(db, thread_id, request_msg_id)
                if request_seq is None:
                    continue

        # New post-compaction markers are appended after the compaction event
        # and clear when their request gets an assistant response.  Keep the
        # older marker/event-sequence checks for legacy pre-migration markers
        # that did not carry a usable request_msg_id.
        if request_seq is not None:
            if request_seq < current_start_seq:
                continue
            if _has_later_assistant_response(int(request_seq)):
                continue
            return marker

        if marker_seq <= current_start_seq:
            continue
        if marker_seq <= current_compaction_event_seq:
            continue
        return marker
    return None


def has_effective_thread_compaction_summary_in_progress(db: ThreadsDB, thread_id: str) -> bool:
    """Return whether an automatic summary compaction request is pending."""

    return latest_effective_thread_compaction_summary_in_progress(db, thread_id) is not None


def current_compaction_start_event_seq(db: ThreadsDB, thread_id: str) -> Optional[int]:
    """Return the latest raw compaction start event_seq, if any."""

    latest = latest_thread_compaction(db, thread_id)
    if not latest:
        return None
    try:
        return int(latest.get('start_event_seq'))
    except Exception:
        return None


def current_effective_compaction_start_event_seq(db: ThreadsDB, thread_id: str) -> Optional[int]:
    """Return the current effective provider-context start event_seq, if any."""

    latest = latest_effective_thread_compaction(db, thread_id)
    if not latest:
        return None
    try:
        return int(latest.get('start_event_seq'))
    except Exception:
        return None


def filter_messages_for_compaction_provider_context(
    db: ThreadsDB,
    thread_id: str,
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return provider-view messages after the latest compaction start.

    UI/raw history intentionally remains full.  This helper is only for
    provider/API context construction.  System messages are preserved so the
    thread's standing instructions survive compaction even when they were
    created before the selected start message.
    """

    messages = [m for m in messages if not (isinstance(m, dict) and m.get('answer_user_preserve_turn'))]

    start_seq = current_effective_compaction_start_event_seq(db, thread_id)
    if start_seq is None:
        return list(messages)

    # Snapshots created before event_seq was persisted on messages can still
    # be filtered by msg_id.  Query lazily once for all messages in the view.
    seq_by_msg_id: Dict[str, int] = {}
    missing_seq = [m.get('msg_id') for m in messages if isinstance(m, dict) and m.get('event_seq') is None and isinstance(m.get('msg_id'), str)]
    if missing_seq:
        try:
            cur = db.conn.execute(
                "SELECT msg_id, event_seq FROM events WHERE thread_id=? AND type='msg.create'",
                (thread_id,),
            )
            for msg_id, event_seq in cur.fetchall():
                if msg_id:
                    seq_by_msg_id[str(msg_id)] = int(event_seq)
        except Exception:
            seq_by_msg_id = {}

    filtered: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get('role') == 'system':
            filtered.append(m)
            continue
        seq_val = m.get('event_seq')
        if seq_val is None and isinstance(m.get('msg_id'), str):
            seq_val = seq_by_msg_id.get(str(m.get('msg_id')))
        try:
            seq_int = int(seq_val)
        except Exception:
            continue
        if seq_int >= int(start_seq):
            filtered.append(m)
    return filtered


def _normalize_compaction_selector(selector: Optional[str]) -> str:
    value = (selector or '').strip()
    return value or 'last_message'


def _compaction_candidate_messages(db: ThreadsDB, thread_id: str) -> List[tuple[int, str, Dict[str, Any]]]:
    skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    cur = db.conn.execute(
        "SELECT event_seq, msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
        (thread_id,),
    )
    rows: List[tuple[int, str, Dict[str, Any]]] = []
    for event_seq, msg_id, payload_json in cur.fetchall():
        if not msg_id or str(msg_id) in skipped or str(msg_id) in deleted:
            continue
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        rows.append((int(event_seq), str(msg_id), payload))
    return rows


def _tool_call_ids_from_payload(payload: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    tool_calls = payload.get('tool_calls')
    if not isinstance(tool_calls, list):
        return ids
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tcid = tc.get('id') or tc.get('tool_call_id')
        if not tcid and isinstance(tc.get('function'), dict):
            tcid = tc['function'].get('id')
        if isinstance(tcid, str) and tcid:
            ids.append(tcid)
    return ids


def _compaction_protocol_rejection_reason(
    selected: tuple[int, str, Dict[str, Any]],
    candidates: List[tuple[int, str, Dict[str, Any]]],
    *,
    pending_tool_call_id: Optional[str] = None,
    pending_tool_parent_msg_id: Optional[str] = None,
) -> Optional[str]:
    """Return why a selected start would break provider tool-call protocol.

    ``ThreadRunner._enforce_assistant_toolcall_protocol`` can safely drop old
    malformed assistant/tool fragments from provider context, but compaction is
    a durable user-visible boundary.  Reject starts that would select only part
    of an otherwise well-formed assistant/tool result block; that would make the
    selected start message itself disappear from the provider prompt after
    sanitization, which is surprising and not a useful compaction boundary.
    """

    ordered = sorted(
        (row for row in candidates if not row[2].get('no_api')),
        key=lambda row: row[0],
    )
    try:
        selected_index = ordered.index(selected)
    except ValueError:
        selected_index = -1

    if selected_index >= 0 and selected[2].get('role') == 'tool':
        preceding_assistant: Optional[tuple[int, str, Dict[str, Any]]] = None
        preceding_tool_ids: set[str] = set()
        selected_tcid = selected[2].get('tool_call_id')
        if isinstance(selected_tcid, str) and selected_tcid:
            preceding_tool_ids.add(selected_tcid)
        j = selected_index - 1
        while j >= 0:
            row = ordered[j]
            role = row[2].get('role')
            if role == 'tool':
                tcid = row[2].get('tool_call_id')
                if isinstance(tcid, str) and tcid:
                    preceding_tool_ids.add(tcid)
                j -= 1
                continue
            if role == 'assistant' and _tool_call_ids_from_payload(row[2]):
                preceding_assistant = row
            break
        if preceding_assistant is not None:
            expected = set(_tool_call_ids_from_payload(preceding_assistant[2]))
            if preceding_tool_ids and preceding_tool_ids.issubset(expected):
                return "starts inside an assistant/tool result block"

    selected_role = selected[2].get('role')
    if selected_role == 'assistant':
        expected_ids = _tool_call_ids_from_payload(selected[2])
        if expected_ids:
            remaining = set(expected_ids)
            idx = selected_index + 1
            while remaining:
                row = ordered[idx] if 0 <= idx < len(ordered) else None
                if row is None or row[2].get('role') != 'tool':
                    if (
                        pending_tool_call_id
                        and pending_tool_parent_msg_id
                        and selected[1] == pending_tool_parent_msg_id
                        and remaining == {pending_tool_call_id}
                    ):
                        return None
                    return "assistant tool_calls are not followed by all required tool results"
                tcid = row[2].get('tool_call_id')
                if not isinstance(tcid, str) or tcid not in remaining:
                    return "assistant tool_calls are followed by an unexpected tool result"
                remaining.remove(tcid)
                idx += 1

    return None


def resolve_compaction_start_message(
    db: ThreadsDB,
    thread_id: str,
    selector: Optional[str] = None,
    *,
    pending_tool_call_id: Optional[str] = None,
    pending_tool_parent_msg_id: Optional[str] = None,
) -> CompactionStartResolution:
    """Resolve a compaction start selector to a valid user/assistant message.

    Supported selectors are an explicit ``msg_id``, ``last_user``,
    ``last_llm``, and omitted/``last_message``.  Tool/system/no_api/skipped
    messages are not valid compaction starts for the MVP.
    """

    if db.get_thread(thread_id) is None:
        return CompactionStartResolution(False, _normalize_compaction_selector(selector), None, None, None, f"Thread not found: {thread_id}")

    normalized = _normalize_compaction_selector(selector)
    current_start = current_effective_compaction_start_event_seq(db, thread_id)
    min_event_seq = int(current_start) if current_start is not None else -1
    candidates = _compaction_candidate_messages(db, thread_id)

    def _valid(row: tuple[int, str, Dict[str, Any]]) -> bool:
        event_seq, _msg_id, payload = row
        role = payload.get('role')
        if event_seq <= min_event_seq:
            return False
        if payload.get('no_api') or payload.get('answer_user_preserve_turn'):
            return False
        return role in ('user', 'assistant')

    selected: Optional[tuple[int, str, Dict[str, Any]]] = None
    if normalized in ('last_message', 'last_user', 'last_llm'):
        role_filter = None
        if normalized == 'last_user':
            role_filter = 'user'
        elif normalized == 'last_llm':
            role_filter = 'assistant'
        for row in reversed(candidates):
            if not _valid(row):
                continue
            if role_filter is not None and row[2].get('role') != role_filter:
                continue
            selected = row
            break
        if selected is None:
            return CompactionStartResolution(False, normalized, None, None, None, f"No valid compaction start message found for selector: {normalized}")
    else:
        for row in candidates:
            if row[1] == normalized:
                selected = row
                break
        if selected is None:
            return CompactionStartResolution(False, normalized, None, None, None, f"Message not found or not active: {normalized}")
        if not _valid(selected):
            role = selected[2].get('role')
            if selected[0] <= min_event_seq:
                reason = "would not reduce the current provider context"
            elif selected[2].get('no_api'):
                reason = "message is hidden from provider APIs"
            else:
                reason = f"message role is not a valid start role: {role!r}"
            return CompactionStartResolution(False, normalized, selected[1], selected[0], role if isinstance(role, str) else None, f"Invalid compaction start: {reason}")

    event_seq, msg_id, payload = selected
    role = payload.get('role')
    protocol_reason = _compaction_protocol_rejection_reason(
        selected,
        candidates,
        pending_tool_call_id=pending_tool_call_id,
        pending_tool_parent_msg_id=pending_tool_parent_msg_id,
    )
    if protocol_reason:
        return CompactionStartResolution(
            False,
            normalized,
            msg_id,
            event_seq,
            role if isinstance(role, str) else None,
            f"Invalid compaction start: {protocol_reason}",
        )
    return CompactionStartResolution(True, normalized, msg_id, event_seq, role if isinstance(role, str) else None, "ok")


def commit_thread_compaction(
    db: ThreadsDB,
    thread_id: str,
    selector: Optional[str] = None,
    *,
    created_by: str,
    tool_call_id: Optional[str] = None,
    committed_from_msg_id: Optional[str] = None,
) -> CompactionCommitResult:
    """Append a ``thread.compaction`` event that sets provider-context start."""

    resolution = resolve_compaction_start_message(
        db,
        thread_id,
        selector,
        pending_tool_call_id=tool_call_id,
        pending_tool_parent_msg_id=committed_from_msg_id,
    )
    if not resolution.success:
        return CompactionCommitResult(False, resolution.selector, resolution.msg_id, resolution.event_seq, None, resolution.message)

    payload = {
        'start_msg_id': resolution.msg_id,
        'start_event_seq': resolution.event_seq,
        'selector': resolution.selector,
        'created_by': created_by,
    }
    if tool_call_id:
        payload['tool_call_id'] = tool_call_id
    if committed_from_msg_id:
        payload['committed_from_msg_id'] = committed_from_msg_id

    event_seq = db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_=COMPACTION_EVENT_TYPE,
        payload=payload,
    )
    return CompactionCommitResult(
        True,
        resolution.selector,
        resolution.msg_id,
        resolution.event_seq,
        int(event_seq),
        f"Compaction committed; provider context now starts at {resolution.msg_id[-8:] if resolution.msg_id else 'unknown'}.",
    )


def append_auto_compaction_summary_request(
    db: ThreadsDB,
    thread_id: str,
    *,
    content: str = AUTO_COMPACTION_SUMMARY_REQUEST,
    selector: str = 'last_llm',
) -> CompactionSummaryRequestResult:
    """Commit an automatic compaction boundary, then append its summary request."""

    if latest_effective_thread_compaction_summary_in_progress(db, thread_id) is not None:
        return CompactionSummaryRequestResult(
            False,
            None,
            None,
            "Automatic summary compaction request already pending.",
        )

    compaction = commit_thread_compaction(
        db,
        thread_id,
        selector,
        created_by='auto_compaction',
    )
    if not compaction.success:
        return CompactionSummaryRequestResult(
            False,
            None,
            None,
            compaction.message,
            compaction,
        )

    request_msg_id = append_message(
        db,
        thread_id,
        'user',
        content,
        extra={
            **_compaction_summary_request_extra(created_by='auto_compaction'),
            'auto_compaction_request': True,
        },
    )
    marker_seq = append_compaction_summary_in_progress(
        db,
        thread_id,
        request_msg_id=request_msg_id,
        created_by='auto_compaction',
    )
    return CompactionSummaryRequestResult(
        True,
        request_msg_id,
        marker_seq,
        "Automatic summary compaction request appended.",
        compaction,
    )


def append_compaction_summary_request(
    db: ThreadsDB,
    thread_id: str,
    *,
    created_by: str = 'user_command',
    content: str = COMPACTION_SUMMARY_REQUEST,
    selector: Optional[str] = None,
) -> str:
    """Commit compaction, then append a normal model-visible summary request."""

    compaction = commit_thread_compaction(
        db,
        thread_id,
        selector,
        created_by=created_by,
    )
    if not compaction.success:
        raise ValueError(compaction.message)

    return append_message(
        db,
        thread_id,
        'user',
        content,
        extra=_compaction_summary_request_extra(created_by=created_by),
    )


def _has_compaction_candidate_after_current_effective_start(db: ThreadsDB, thread_id: str) -> bool:
    start_seq = current_effective_compaction_start_event_seq(db, thread_id)
    if start_seq is None:
        for _event_seq, _msg_id, payload in _compaction_candidate_messages(db, thread_id):
            if payload.get('no_api') or payload.get('auto_compaction_request') or payload.get('answer_user_preserve_turn'):
                continue
            if payload.get('role') in ('user', 'assistant'):
                return True
        return False

    min_event_seq = int(start_seq)
    last_llm_seq: Optional[int] = None
    last_non_request_user_seq: Optional[int] = None
    for event_seq, _msg_id, payload in _compaction_candidate_messages(db, thread_id):
        if event_seq <= min_event_seq:
            continue
        if payload.get('no_api') or payload.get('auto_compaction_request') or payload.get('answer_user_preserve_turn'):
            continue
        role = payload.get('role')
        if role == 'assistant':
            last_llm_seq = event_seq
        elif role == 'user':
            last_non_request_user_seq = event_seq
    return last_non_request_user_seq is not None



def _repl_message_view(message: Dict[str, Any], db: ThreadsDB, thread_id: str) -> Dict[str, Any]:
    """Return the consumer-facing REPL representation of one usable message."""

    out: Dict[str, Any] = {
        'msg_id': message.get('msg_id'),
        'role': message.get('role'),
        'content': message.get('content', ''),
    }
    seq = message.get('event_seq')
    if seq is None:
        seq = _get_event_seq_for_msg_id(db, thread_id, message.get('msg_id'))
    if seq is not None:
        try:
            out['event_seq'] = int(seq)
        except Exception:
            out['event_seq'] = seq
    if message.get('ts') is not None:
        out['ts'] = message.get('ts')

    # Preserve common protocol fields so tool turns can be inspected exactly
    # enough from the REPL without exposing unrelated internal bookkeeping.
    for key in ('name', 'tool_call_id', 'tool_calls'):
        if key in message:
            out[key] = message.get(key)
    return out


def _messages_by_role(messages: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {
        'system': [],
        'user': [],
        'assistant': [],
        'tool': [],
    }
    for message in messages:
        role = message.get('role')
        if isinstance(role, str):
            grouped.setdefault(role, []).append(message)
    return grouped


def _effective_thread_compactions_for_repl(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    """Return compaction markers still present in the effective thread view."""

    erased_ranges = _continue_erased_event_ranges(db, thread_id)
    skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    out: List[Dict[str, Any]] = []
    for marker in list_thread_compactions(db, thread_id):
        try:
            marker_seq = int(marker.get('event_seq'))
        except Exception:
            continue
        if _event_erased_by_continue(marker_seq, erased_ranges):
            continue
        start_msg_id = marker.get('start_msg_id')
        if isinstance(start_msg_id, str) and (start_msg_id in skipped or start_msg_id in deleted):
            continue
        out.append(marker)
    return out


def _repl_context_compactions(db: ThreadsDB, thread_id: str) -> List[Dict[str, Any]]:
    """Return consumer-facing compaction marker summaries."""

    current = latest_effective_thread_compaction(db, thread_id)
    current_seq: Optional[int] = None
    if current is not None:
        try:
            current_seq = int(current.get('event_seq'))
        except Exception:
            current_seq = None

    out: List[Dict[str, Any]] = []
    for marker in _effective_thread_compactions_for_repl(db, thread_id):
        try:
            marker_seq: Optional[int] = int(marker.get('event_seq'))
        except Exception:
            marker_seq = None
        start_seq = marker.get('start_event_seq')
        try:
            start_seq_out: Any = int(start_seq) if start_seq is not None else None
        except Exception:
            start_seq_out = start_seq
        out.append({
            'marker_event_seq': marker_seq,
            'current_prompt_starts_at_msg_id': marker.get('start_msg_id'),
            'current_prompt_starts_at_event_seq': start_seq_out,
            'selector_used': marker.get('selector'),
            'created_by': marker.get('created_by'),
            'is_current': bool(current_seq is not None and marker_seq == current_seq),
        })
    return out


def build_repl_thread_context(db: ThreadsDB, thread_id: str) -> Dict[str, Any]:
    """Build a consumer-friendly context object for thread REPL use.

    The returned structure contains the usable effective thread transcript and
    the same compacted prompt slice that provider/API context uses.  Hidden
    local-only messages (``no_api``), deleted messages, and messages skipped by
    ``/continue`` are excluded.  Tool output is passed through the normal
    provider-context compaction filtering and API sanitization path rather than
    the removed model-visible source/status helpers.
    """

    if db.get_thread(thread_id) is None:
        raise ValueError(f"Thread not found: {thread_id}")

    snapshot = create_snapshot(db, thread_id)
    raw_messages = snapshot.get('messages', []) if isinstance(snapshot, dict) else []
    usable_messages = [m for m in raw_messages if isinstance(m, dict) and not m.get('no_api')]
    current_prompt_raw = filter_messages_for_compaction_provider_context(db, thread_id, usable_messages)

    class _ContextSanitizer:
        def __init__(self, db: ThreadsDB, thread_id: str) -> None:
            self.db = db
            self.thread_id = thread_id
            self.llm = None

        def _get_tool_call_id_normalization_strategy(self, model_key=None):
            return None

        def _mask_secrets_heuristic(self, text: str) -> str:
            from .runner import ThreadRunner

            return ThreadRunner._mask_secrets_heuristic(self, text)

        def _filter_tool_output(self, text: str, *, mask_secrets: bool = True) -> str:
            from .runner import ThreadRunner

            return ThreadRunner._filter_tool_output(self, text, mask_secrets=mask_secrets)

        def _enforce_assistant_toolcall_protocol(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            from .runner import ThreadRunner

            return ThreadRunner._enforce_assistant_toolcall_protocol(self, messages)

    sanitizer = _ContextSanitizer(db, thread_id)

    def _sanitize_tool_output_for_repl_message(message: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(message)
        if out.get('role') != 'tool':
            return out
        content = out.get('content')
        if not isinstance(content, str):
            return out
        try:
            from .tools_config import get_thread_tools_config

            tools_cfg = get_thread_tools_config(db, thread_id)
            mask_secrets = not bool(getattr(tools_cfg, 'allow_raw_tool_output', False))
        except Exception:
            mask_secrets = True
        try:
            out['content'] = sanitizer._filter_tool_output(content, mask_secrets=mask_secrets)
        except Exception:
            pass
        return out

    def _sanitize_for_repl_context(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        try:
            from .runner import ThreadRunner

            sanitized = ThreadRunner._sanitize_messages_for_api(sanitizer, messages)
        except Exception:
            return list(messages)
        if len(sanitized) != len(messages):
            return sanitized
        out: List[Dict[str, Any]] = []
        for original, clean in zip(messages, sanitized):
            if not isinstance(original, dict) or not isinstance(clean, dict):
                out.append(clean)
                continue
            clean_copy = dict(clean)
            for key in ('msg_id', 'event_seq', 'ts'):
                if key not in clean_copy and key in original:
                    clean_copy[key] = original.get(key)
            out.append(clean_copy)
        return out

    all_messages_raw = [_sanitize_tool_output_for_repl_message(m) for m in usable_messages]
    current_prompt_sanitized = _sanitize_for_repl_context(current_prompt_raw)

    prompt_ids = {
        m.get('msg_id')
        for m in current_prompt_raw
        if isinstance(m, dict) and isinstance(m.get('msg_id'), str)
    }
    all_messages = [_repl_message_view(m, db, thread_id) for m in all_messages_raw]
    current_prompt_messages = [
        _repl_message_view(m, db, thread_id)
        for m in current_prompt_sanitized
        if isinstance(m, dict)
    ]
    older_messages_not_in_prompt = [
        _repl_message_view(m, db, thread_id)
        for m in all_messages_raw
        if m.get('msg_id') not in prompt_ids
    ]

    messages_by_id = {
        message.get('msg_id'): message
        for message in all_messages
        if isinstance(message.get('msg_id'), str)
    }
    messages_by_role = _messages_by_role(all_messages)
    max_seq = db.max_event_seq(thread_id)

    return {
        'thread': {
            'thread_id': thread_id,
            'loaded_at': _utcnow_iso(),
            'loaded_through_event_seq': max_seq,
            'message_count': len(all_messages),
            'visibility_note': 'Contains the thread messages available for model use; hidden/local-only content is excluded.',
        },
        'how_to_use': (
            "Use all_messages for the full usable transcript, "
            "current_prompt_messages for the messages currently in the model prompt, "
            "older_messages_not_in_prompt for earlier usable context omitted after compaction, "
            "messages_by_id[get_id] for exact messages, and messages_by_role for role-based browsing."
        ),
        'all_messages': all_messages,
        'current_prompt_messages': current_prompt_messages,
        'older_messages_not_in_prompt': older_messages_not_in_prompt,
        'messages_by_id': messages_by_id,
        'messages_by_role': messages_by_role,
        'compactions': _repl_context_compactions(db, thread_id),
        'context_files': {},
    }


def maybe_auto_compact_thread(
    db: ThreadsDB,
    thread_id: str,
    *,
    threshold_tokens: Optional[int],
    context_tokens: Optional[int] = None,
    selector: str = 'last_llm',
    summary_mode: Optional[bool] = None,
    checkpoint_resume: Optional[bool] = None,
) -> AutoCompactionResult:
    """Maybe perform automatic compaction at a safe turn boundary.

    Callers invoke this at a user-turn/scheduler boundary, and it only acts
    when the effective provider-context token estimate is at or above
    ``threshold_tokens``.  In summary mode it first commits the safe
    compaction boundary, then appends one normal model-visible request asking
    the assistant to summarize the freshly compacted thread.  Direct mode
    still goes through the shared compaction helper and all normal selector
    validation/no-op handling.
    """

    if summary_mode is None:
        summary_mode = auto_compact_summary_enabled()

    if threshold_tokens is None:
        return AutoCompactionResult(False, False, int(context_tokens or 0), None, None, "Auto compaction disabled.")
    try:
        threshold_int = int(threshold_tokens)
    except Exception:
        return AutoCompactionResult(False, False, int(context_tokens or 0), None, None, "Auto compaction disabled.")
    if threshold_int <= 0:
        return AutoCompactionResult(False, False, int(context_tokens or 0), None, None, "Auto compaction disabled.")

    if context_tokens is None:
        try:
            from .token_count import provider_context_token_stats

            stats = provider_context_token_stats(db, thread_id)
            context_tokens = int(stats.get('context_tokens') or 0)
        except Exception as e:
            return AutoCompactionResult(False, False, 0, threshold_int, None, f"Auto compaction token estimate failed: {e}")

    context_int = int(context_tokens or 0)
    if context_int < threshold_int:
        return AutoCompactionResult(False, False, context_int, threshold_int, None, "Auto compaction threshold not reached.")

    if summary_mode:
        if not _has_compaction_candidate_after_current_effective_start(db, thread_id):
            return AutoCompactionResult(
                False,
                True,
                context_int,
                threshold_int,
                None,
                "No new provider-visible user or assistant message after current compaction start.",
            )
        if checkpoint_resume is None:
            checkpoint_resume = False
        content = AUTO_COMPACTION_SUMMARY_REQUEST if checkpoint_resume else COMPACTION_SUMMARY_REQUEST
        summary_result = append_auto_compaction_summary_request(
            db,
            thread_id,
            selector=selector,
            content=content,
        )
        return AutoCompactionResult(
            bool(summary_result.success),
            True,
            context_int,
            threshold_int,
            summary_result.compaction,
            summary_result.message,
        )

    result = commit_thread_compaction(
        db,
        thread_id,
        selector,
        created_by='auto_compaction',
    )
    return AutoCompactionResult(
        bool(result.success),
        True,
        context_int,
        threshold_int,
        result,
        result.message,
    )


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
        if isinstance(payload, dict) and payload.get('recovery_notice'):
            continue
        # Assistant notes are local/user-visible status updates emitted by the
        # answer-user tool. They are filtered out of provider/API context, so
        # they must not participate in protocol checks such as consecutive
        # assistant-message detection.
        if isinstance(payload, dict) and payload.get('answer_user_preserve_turn'):
            continue
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

    A candidate is only valid if continuing from it would also remove any
    still-unpublished tool calls. Since tool-call state is anchored to the
    parent ``msg.create`` event, a continue point that comes *after* an
    unpublished tool call's parent message is not actually safe: the
    unpublished tool call would remain in the reconstructed state and can block
    future RA1 turns. In that case we must choose an earlier point so the
    parent message itself gets skipped by ``continue_thread``.

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

    # Find messages with unpublished tool calls.
    #
    # ``continue_thread`` only marks msg.create events after the continue point
    # as skipped; it does not directly mutate tool_call.* events. Therefore a
    # continue point is only safe if it lies *before* the parent message of any
    # unpublished tool call, so that the skipped parent message removes the
    # stale tool-call state from reconstruction.
    unpublished_tc_msg_ids = set()
    earliest_unpublished_parent_seq: Optional[int] = None
    for tc in tc_states.values():
        if not tc.published:
            unpublished_tc_msg_ids.add(tc.parent_msg_id)
            if earliest_unpublished_parent_seq is None or tc.parent_event_seq < earliest_unpublished_parent_seq:
                earliest_unpublished_parent_seq = tc.parent_event_seq

    # Iterate backward to find a good continue point
    for msg_id, pj, event_seq in rows:
        # If an unpublished tool call exists, the continue point must be before
        # the earliest such parent message. Otherwise the stale tool-call state
        # survives the continue and can keep the thread blocked in TC2-TC5.
        if earliest_unpublished_parent_seq is not None and int(event_seq) >= earliest_unpublished_parent_seq:
            continue

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
        if no_api or payload.get('answer_user_preserve_turn'):
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
            lease_until = row['lease_until']
            now_iso = _utcnow_iso()
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
            lease_until = row['lease_until']
            now_iso = _utcnow_iso()
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

        try:
            payload = json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get('preserve_on_continue'):
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

def continue_child_thread(
    db: ThreadsDB,
    manager_thread_id: str,
    child_thread_id: str,
    msg_id: Optional[str] = None,
) -> ContinueResult:
    """Continue/repair a descendant thread owned by a manager thread."""

    manager = (manager_thread_id or "").strip()
    child = (child_thread_id or "").strip()
    if not manager:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message="manager_thread_id is required",
        )
    if not child:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message="child_thread_id is required",
        )
    if db.get_thread(manager) is None:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message=f"manager thread not found: {manager}",
        )
    if db.get_thread(child) is None:
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message=f"child thread not found: {child}",
        )
    if not is_descendant_thread(db, manager, child):
        return ContinueResult(
            success=False,
            continue_from_msg_id=None,
            skipped_msg_ids=[],
            message="target thread must be a child or descendant of the calling thread",
        )
    return continue_thread(db, child, msg_id=msg_id)


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


_SNAPSHOT_INCREMENTAL_IGNORED_EVENT_TYPES = {
    'stream.open',
    'stream.delta',
    'stream.close',
    'tool_call.approval',
    'tool_call.execution_started',
    'tool_call.summary',
    'tool_call.finished',
    'tool_call.output_approval',
    'control.interrupt',
    'thread.compaction',
    'thread.compaction_context_length',
    'thread.compaction_summary_in_progress',
    'thread.recovery',
    'model.switch',
    'runtime.config',
    'sandbox.config',
    'thread.scheduling',
}


def _snapshot_message_from_create_event(ev) -> Dict[str, Any]:
    try:
        payload = json.loads(ev["payload_json"]) if isinstance(ev["payload_json"], str) else (ev["payload_json"] or {})
    except Exception:
        payload = {}
    msg = dict(payload) if isinstance(payload, dict) else {}
    msg["msg_id"] = ev["msg_id"]
    msg["role"] = msg.get("role")
    ts_val = ev["ts"]
    if ts_val is not None:
        msg["ts"] = ts_val
    event_seq_val = ev["event_seq"]
    if event_seq_val is not None:
        try:
            msg["event_seq"] = int(event_seq_val)
        except Exception:
            msg["event_seq"] = event_seq_val
    return msg


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
    th = db.get_thread(thread_id)
    if th and th.snapshot_json and th.snapshot_last_event_seq >= 0:
        try:
            snap = json.loads(th.snapshot_json)
            messages = snap.get("messages")
            if not isinstance(snap, dict) or not isinstance(messages, list):
                raise ValueError("invalid snapshot")
            cur = db.conn.execute(
                "SELECT * FROM events WHERE thread_id=? AND event_seq>? ORDER BY event_seq ASC",
                (thread_id, int(th.snapshot_last_event_seq)),
            )
            tail = cur.fetchall()
            if not tail:
                return snap
            if tail and all(
                row["type"] == "msg.create" or row["type"] in _SNAPSHOT_INCREMENTAL_IGNORED_EVENT_TYPES
                for row in tail
            ):
                messages = list(messages)
                tail_messages = []
                for ev in tail:
                    if ev["type"] != "msg.create":
                        continue
                    msg = _snapshot_message_from_create_event(ev)
                    messages.append(msg)
                    tail_messages.append(msg)
                snap["messages"] = messages
                if tail_messages:
                    try:
                        from .token_count import extend_snapshot_token_stats  # type: ignore

                        snap["token_stats"] = extend_snapshot_token_stats(snap, tail_messages)
                    except Exception:
                        pass
                last_seq = tail[-1]["event_seq"]
                db.conn.execute("UPDATE threads SET snapshot_json=?, snapshot_last_event_seq=? WHERE thread_id=?",
                                (json.dumps(snap), last_seq, thread_id))
                return snap
        except Exception:
            pass

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


def get_thread_status(db: ThreadsDB, thread_id: str) -> str:
    """Return the real-time status of a thread.

    Status values:
    - "streaming": Thread has an active (non-expired) lease in open_streams
    - "runnable": Thread has pending work (RA1/RA2/RA3) but is not streaming
    - "idle": Thread has no active lease and no pending work

    This function properly checks lease expiration, unlike the static
    'status' column in the threads table which can become stale after crashes.
    """
    # Check for active (non-expired) lease
    try:
        row_open = db.current_open(thread_id)
        if row_open:
            # sqlite3.Row uses [] access, not .get()
            lease_until = row_open['lease_until']
            if lease_until:
                now_iso = _utcnow_iso()
                if lease_until > now_iso:
                    return "streaming"
    except Exception:
        pass

    # Check for pending work
    if is_thread_runnable(db, thread_id):
        return "runnable"

    return "idle"


def get_thread_statuses_bulk(db: ThreadsDB, thread_ids: list[str], *, skip_runnability: bool = False) -> dict[str, str]:
    """Return real-time status for multiple threads efficiently.

    Uses batch queries where possible:
    1. Single query to find all streaming threads (active leases)
    2. Checks runnability for remaining threads (uses internal cache)

    Status values: "streaming", "runnable", "idle"

    Args:
        skip_runnability: When True, skip the expensive per-thread
            runnability checks.  Streaming detection still works.
    """
    from .tool_state import discover_runner_actionable_cached

    result: dict[str, str] = {tid: "idle" for tid in thread_ids}

    # Batch query: find all threads with active (non-expired) leases
    streaming_set: set[str] = set()
    try:
        now_iso = _utcnow_iso()
        cur = db.conn.execute(
            "SELECT thread_id FROM open_streams WHERE lease_until > ?",
            (now_iso,)
        )
        for row in cur.fetchall():
            tid = row[0]
            if tid in result:
                streaming_set.add(tid)
                result[tid] = "streaming"
    except Exception:
        pass

    # Check runnability for non-streaming threads
    # discover_runner_actionable_cached has internal caching per thread
    if not skip_runnability:
        for tid in thread_ids:
            if tid not in streaming_set:
                try:
                    if discover_runner_actionable_cached(db, tid) is not None:
                        result[tid] = "runnable"
                except Exception:
                    pass

    return result


def get_thread_auto_approval_status(db: ThreadsDB, thread_id: str) -> bool:
    """Return whether global tool auto-approval is active for a thread."""
    try:
        cur = db.conn.execute(
            """SELECT payload_json FROM events
               WHERE thread_id=? AND type='tool_call.approval'
               ORDER BY event_seq DESC""",
            (thread_id,),
        )
    except Exception:
        return False

    for row in cur.fetchall():
        try:
            payload_json = row["payload_json"]
        except Exception:
            try:
                payload_json = row[0]
            except Exception:
                payload_json = None
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except Exception:
            payload = {}
        decision = payload.get("decision") if isinstance(payload, dict) else None
        if decision == "global_approval":
            return True
        if decision == "revoke_global_approval":
            return False
    return False


# --------- Query helpers (expose common SQL as API) -------------------------
def list_threads(db: ThreadsDB) -> list[ThreadRow]:
    """List all threads in the database.

    Args:
        db: ThreadsDB instance for database operations.

    Returns:
        List of ThreadRow objects for all threads.
    """
    try:
        cur = db.conn.execute(
            "SELECT thread_id, name, short_recap, status, NULL AS snapshot_json, "
            "snapshot_last_event_seq, initial_model_key, depth, created_at "
            "FROM threads"
        )
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
                         models_path: str = "models.json",
                         all_models_path: str | None = None) -> None:
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
            concrete_model_info = _get_concrete_model_info(model_key, models_path,
                                                           all_models_path=all_models_path)
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


def _nearest_model_switch_payload(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the nearest ancestor's model.switch payload (including self).

    Walks up the parent chain to find the first model.switch event.
    Used by duplicate_thread to copy the active model configuration.

    Returns:
        The payload dict if found, or None if no model switch is configured.
    """
    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='model.switch' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if row:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload:
                return payload
        # Walk up to parent
        p_row = db.conn.execute("SELECT parent_id FROM children WHERE child_id=?", (tid,)).fetchone()
        tid = p_row[0] if p_row else None
    return None


def _nearest_working_dir_payload(db: ThreadsDB, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the nearest ancestor's thread.config payload (including self).

    Walks up the parent chain to find the first thread.config event with
    a working_dir setting. Used by duplicate_thread to copy inherited configs.

    Returns:
        The payload dict if found, or None if no working directory is configured.
    """
    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='thread.config' ORDER BY event_seq DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if row:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload.get('working_dir'):
                return payload
        # Walk up to parent
        p_row = db.conn.execute("SELECT parent_id FROM children WHERE child_id=?", (tid,)).fetchone()
        tid = p_row[0] if p_row else None
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


def is_descendant_thread(db: ThreadsDB, ancestor_id: str, thread_id: str) -> bool:
    """Return True if ``thread_id`` is a strict descendant of ``ancestor_id``."""

    if not ancestor_id or not thread_id or ancestor_id == thread_id:
        return False
    current = thread_id
    seen: set[str] = set()
    for _ in range(2048):
        if current in seen:
            return False
        seen.add(current)
        parent = get_parent(db, current)
        if parent is None:
            return False
        if parent == ancestor_id:
            return True
        current = parent
    return False


def send_message_to_child_thread(
    db: ThreadsDB,
    manager_thread_id: str,
    child_thread_id: str,
    message: str,
    *,
    require_idle: bool = True,
) -> str:
    """Append a normal user message from a manager to a descendant thread.

    This is intentionally a small primitive: it does not wait for the child,
    grant tools, alter scheduling, or implement a manager framework.  The target
    must be a descendant of the manager thread so managers cannot steer
    unrelated threads.
    """

    manager = (manager_thread_id or "").strip()
    child = (child_thread_id or "").strip()
    text = str(message or "")
    if not manager:
        raise ValueError("manager_thread_id is required")
    if not child:
        raise ValueError("child_thread_id is required")
    if not text.strip():
        raise ValueError("message is required")
    if db.get_thread(manager) is None:
        raise ValueError(f"manager thread not found: {manager}")
    if db.get_thread(child) is None:
        raise ValueError(f"child thread not found: {child}")
    if not is_descendant_thread(db, manager, child):
        raise ValueError("target thread must be a child or descendant of the calling thread")
    status = get_thread_status(db, child)
    if require_idle and status != "idle":
        raise ValueError(f"target thread is not idle (status={status}); wait for it before sending guidance")
    msg_id = append_message(
        db,
        child,
        "user",
        text,
        extra={
            "origin": "manager_message",
            "from_thread_id": manager,
        },
    )
    create_snapshot(db, child)
    return msg_id


def list_active_threads(db: ThreadsDB, subtree: list[str]) -> list[str]:
    """Return list of thread_ids that are currently running or runnable."""
    active: list[str] = []
    for tid in subtree:
        is_running = False
        try:
            row_open = db.current_open(tid)
            if row_open:
                # Only consider thread running if lease hasn't expired
                lease_until = row_open['lease_until']
                now_iso = _utcnow_iso()
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


async def wait_thread_settled(db: ThreadsDB, thread_id: str, poll_sec: float = 0.1, quiet_checks: int = 3) -> str:
    """Wait until a thread reaches a stable non-running state.

    Unlike :func:`wait_subtree_idle`, which only answers whether a subtree has
    any *running or runnable* work, this helper preserves the distinction
    between different non-running states.  That matters for callers that need
    to know whether a thread is genuinely waiting for user input or is blocked
    on approval/state cleanup.

    Returns one of the coarse :func:`eggthreads.tool_state.thread_state`
    values, typically:

    - ``"waiting_user"``
    - ``"waiting_tool_approval"``
    - ``"waiting_output_approval"``
    - ``"paused"``

    The state must remain non-``"running"`` for ``quiet_checks`` consecutive
    polls before it is returned.
    """
    import asyncio
    from .tool_state import thread_state as _thread_state

    stable = 0
    last_state = "running"
    while True:
        state = _thread_state(db, thread_id)
        if state != "running":
            if state == last_state:
                stable += 1
            else:
                stable = 1
                last_state = state
            if stable >= quiet_checks:
                return state
        else:
            stable = 0
            last_state = state
        await asyncio.sleep(poll_sec)

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


def set_context_limit(db: ThreadsDB, thread_id: str, max_tokens: int, reason: str = "user") -> None:
    """Set the maximum context token limit for a thread.

    Appends a ``thread.context_limit`` event. The runner checks this limit
    before each LLM API call and emits an error if exceeded.

    Args:
        db: ThreadsDB instance.
        thread_id: ID of the thread to configure.
        max_tokens: Maximum context tokens allowed (0 or negative disables limit).
        reason: Human-readable reason for the change.
    """
    import os
    db.append_event(
        event_id=os.urandom(10).hex(),
        thread_id=thread_id,
        type_='thread.context_limit',
        payload={
            'max_tokens': int(max_tokens),
            'reason': reason,
        }
    )


def get_context_limit(db: ThreadsDB, thread_id: str) -> Optional[int]:
    """Get the effective context limit for a thread.

    Resolves by checking ``thread.context_limit`` events for this thread
    and ancestors (inheritance). Returns None if no limit is configured.

    Args:
        db: ThreadsDB instance.
        thread_id: ID of the thread.

    Returns:
        Maximum tokens allowed, or None if not configured.
    """
    tid = thread_id
    seen: set = set()
    while tid and tid not in seen:
        seen.add(tid)
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='thread.context_limit' ORDER BY event_seq DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if row:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                max_tokens = payload.get('max_tokens')
                if isinstance(max_tokens, int) and max_tokens > 0:
                    return max_tokens
        except Exception:
            pass
        # Walk up to parent for inheritance
        try:
            p_row = db.conn.execute("SELECT parent_id FROM children WHERE child_id=?", (tid,)).fetchone()
            tid = p_row[0] if p_row else None
        except Exception:
            tid = None
    return None


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


@dataclass
class ToolCallResult:
    """Result of waiting for a specific tool call to publish (TC6)."""

    thread_id: str
    tool_call_id: str
    state: str
    content: Optional[str]
    finished_reason: Optional[str] = None
    output_decision: Optional[str] = None
    timed_out: bool = False


@dataclass
class ThreadWaitResult:
    """Structured result for waiting on a thread to finish."""

    thread_id: str
    finished: bool
    state: str
    last_assistant_message: str = ""
    short_recap: Optional[str] = None


@dataclass
class _ThreadWaitUnfinishedCache:
    """Per-call cache for an unchanged thread that was already proven unfinished."""

    event_watermark: int
    state: str


@dataclass
class ChildThreadStatus:
    """Manager-visible status for a child/descendant thread."""

    thread_id: str
    name: Optional[str]
    short_recap: Optional[str]
    state: str
    context_tokens: int
    full_thread_tokens: int = 0
    compaction: Optional[Dict[str, Any]] = None
    context_limit: Optional[int] = None
    context_limit_percent: Optional[float] = None
    error_count: int = 0
    recent_errors: Optional[List[Dict[str, Any]]] = None
    assistant_notes: Optional[List[Dict[str, Any]]] = None
    last_event_seq: int = -1
    last_event_ts: Optional[str] = None
    open_invoke_id: Optional[str] = None
    token_stats_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "thread_id": self.thread_id,
            "name": self.name,
            "short_recap": self.short_recap,
            "state": self.state,
            "context_tokens": int(self.context_tokens),
            "full_thread_tokens": int(self.full_thread_tokens),
            "compaction": dict(self.compaction or {"compacted": False}),
            "context_limit": self.context_limit,
            "context_limit_percent": self.context_limit_percent,
            "error_count": int(self.error_count),
            "recent_errors": list(self.recent_errors or []),
            "assistant_notes": list(self.assistant_notes or []),
            "last_event_seq": int(self.last_event_seq),
            "last_event_ts": self.last_event_ts,
            "open_invoke_id": self.open_invoke_id,
        }
        if self.token_stats_error:
            out["token_stats_error"] = self.token_stats_error
        return out


def _truncate_status_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _event_payload_from_row(row: Any) -> Dict[str, Any]:
    try:
        raw = row["payload_json"]
    except Exception:
        raw = None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _error_item_from_event(row: Any) -> Optional[Dict[str, Any]]:
    """Return a compact error item for a known error-like event, if any."""

    try:
        ev_type = str(row["type"] or "")
        event_seq = int(row["event_seq"])
        ts = row["ts"]
        msg_id = row["msg_id"]
        invoke_id = row["invoke_id"]
    except Exception:
        return None

    payload = _event_payload_from_row(row)
    if payload.get("recovery_notice"):
        return None

    def item(category: str, message: Any) -> Optional[Dict[str, Any]]:
        text = _truncate_status_text(message)
        if not text:
            return None
        out: Dict[str, Any] = {
            "event_seq": event_seq,
            "ts": ts,
            "type": ev_type,
            "category": category,
            "message": text,
        }
        if msg_id:
            out["msg_id"] = msg_id
        if invoke_id:
            out["invoke_id"] = invoke_id
        return out

    if ev_type == "msg.create":
        role = payload.get("role")
        content = payload.get("content")
        if isinstance(content, str):
            low = content.lower()
            if role == "system" and (
                "llm/runner error" in low
                or "llm error" in low
                or "context limit exceeded" in low
                or low.startswith("error:")
            ):
                return item("llm", content)
        incomplete_reason = payload.get("incomplete_reason")
        if payload.get("incomplete") or incomplete_reason:
            reason = incomplete_reason or "assistant message marked incomplete"
            return item("llm_stream", reason)

    if ev_type == "stream.delta":
        # ``reason`` is also used for normal reasoning deltas, so only treat it
        # as an error if the text is explicitly error-like.
        reason = payload.get("reason")
        if isinstance(reason, str):
            low = reason.lower()
            if "llm/runner error" in low or "llm error" in low or "context limit exceeded" in low:
                return item("llm", reason)

    if ev_type == "session.lifecycle":
        action = str(payload.get("action") or "")
        error = payload.get("error")
        if error or action.endswith("_error") or action in ("docker_error", "stop_error"):
            return item("session", error or action)

    if ev_type == "tool_call.finished":
        reason = str(payload.get("reason") or "")
        output = payload.get("output")
        if reason and reason not in ("success", "ok"):
            return item("tool", f"tool_call.finished reason={reason}")
        if isinstance(output, str) and output.strip().lower().startswith("error:"):
            return item("tool", output)

    return None


def _recent_thread_errors(db: ThreadsDB, thread_id: str, *, max_errors: int) -> tuple[List[Dict[str, Any]], int]:
    try:
        max_errors = max(0, min(int(max_errors), 20))
    except Exception:
        max_errors = 5
    errors: List[Dict[str, Any]] = []
    count = 0
    try:
        cur = db.conn.execute(
            "SELECT event_seq, ts, type, msg_id, invoke_id, payload_json "
            "FROM events WHERE thread_id=? ORDER BY event_seq DESC",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    for row in rows:
        err = _error_item_from_event(row)
        if err is None:
            continue
        count += 1
        if len(errors) < max_errors:
            errors.append(err)
    return errors, count


def _active_assistant_notes(db: ThreadsDB, thread_id: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Return assistant-note messages in the current unfinished assistant workflow."""

    try:
        limit = max(0, min(int(limit), 20))
    except Exception:
        limit = 5
    if limit <= 0:
        return []

    try:
        skipped, deleted = _compaction_skipped_and_deleted_msg_ids(db, thread_id)
    except Exception:
        skipped, deleted = set(), set()

    try:
        cur = db.conn.execute(
            "SELECT event_seq, ts, msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []

    notes: List[Dict[str, Any]] = []
    for row in rows:
        try:
            msg_id = str(row["msg_id"] or "")
            event_seq = int(row["event_seq"])
            ts = row["ts"]
        except Exception:
            continue
        if msg_id and (msg_id in skipped or msg_id in deleted):
            continue
        payload = _event_payload_from_row(row)
        if payload.get("role") != "assistant":
            continue
        if payload.get("answer_user_preserve_turn"):
            item: Dict[str, Any] = {
                "event_seq": event_seq,
                "ts": ts,
                "msg_id": msg_id,
                "content": _truncate_status_text(payload.get("content"), limit=2000),
            }
            model_key = payload.get("model_key")
            if isinstance(model_key, str) and model_key:
                item["model_key"] = model_key
            tool_call_id = payload.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id:
                item["tool_call_id"] = tool_call_id
            notes.append(item)
            continue

        # A normal assistant message without tool calls is the completed
        # response boundary for the current workflow; older interim notes are
        # no longer active status updates after that point.
        tool_calls = payload.get("tool_calls") or []
        if not tool_calls:
            notes.clear()

    return notes[-limit:]


def _last_event_meta(db: ThreadsDB, thread_id: str) -> tuple[int, Optional[str]]:
    try:
        row = db.conn.execute(
            "SELECT event_seq, ts FROM events WHERE thread_id=? ORDER BY event_seq DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row is not None:
            return int(row[0]), row[1]
    except Exception:
        pass
    return -1, None


def thread_compaction_status(db: ThreadsDB, thread_id: str) -> Dict[str, Any]:
    """Return concise current compaction info for status surfaces."""

    raw_count = len(list_thread_compactions(db, thread_id))
    current = latest_effective_thread_compaction(db, thread_id)
    if current is None:
        return {"compacted": False, "raw_marker_count": raw_count}

    def _int_or_none_local(value: Any) -> Optional[int]:
        try:
            return int(value)
        except Exception:
            return None

    return {
        "compacted": True,
        "current_prompt_start_msg_id": current.get("start_msg_id"),
        "current_prompt_start_event_seq": _int_or_none_local(current.get("start_event_seq")),
        "marker_event_seq": _int_or_none_local(current.get("event_seq")),
        "raw_marker_count": raw_count,
    }


def get_child_thread_status(
    db: ThreadsDB,
    manager_thread_id: str,
    child_thread_id: str,
    *,
    max_errors: int = 5,
) -> ChildThreadStatus:
    """Return status, approximate context length, and recent errors for a descendant.

    The target must be a child or deeper descendant of ``manager_thread_id``;
    this mirrors ``send_message_to_child_thread`` and prevents managers from
    inspecting unrelated threads.
    """

    manager = (manager_thread_id or "").strip()
    child = (child_thread_id or "").strip()
    if not manager:
        raise ValueError("manager_thread_id is required")
    if not child:
        raise ValueError("child_thread_id is required")
    if db.get_thread(manager) is None:
        raise ValueError(f"manager thread not found: {manager}")
    row = db.get_thread(child)
    if row is None:
        raise ValueError(f"child thread not found: {child}")
    if not is_descendant_thread(db, manager, child):
        raise ValueError("target thread must be a child or descendant of the calling thread")

    try:
        from .tool_state import thread_state

        state = thread_state(db, child)
    except Exception:
        state = "unknown"

    context_tokens = 0
    full_thread_tokens = 0
    compaction = thread_compaction_status(db, child)
    token_stats_error: Optional[str] = None
    try:
        from .token_count import thread_token_stats

        stats = thread_token_stats(db, child)
        context_tokens = int(stats.get("context_tokens") or 0)
        full_thread_tokens = int(stats.get("full_thread_tokens") or context_tokens)
    except Exception as e:
        token_stats_error = f"{type(e).__name__}: {e}"

    context_limit = get_context_limit(db, child)
    context_limit_percent: Optional[float] = None
    if isinstance(context_limit, int) and context_limit > 0:
        context_limit_percent = round((float(context_tokens) / float(context_limit)) * 100.0, 2)

    recent_errors, error_count = _recent_thread_errors(db, child, max_errors=max_errors)
    assistant_notes = _active_assistant_notes(db, child)
    last_event_seq, last_event_ts = _last_event_meta(db, child)
    open_invoke_id = current_open_invoke(db, child)

    return ChildThreadStatus(
        thread_id=child,
        name=row.name,
        short_recap=row.short_recap,
        state=state,
        context_tokens=context_tokens,
        full_thread_tokens=full_thread_tokens,
        compaction=compaction,
        context_limit=context_limit,
        context_limit_percent=context_limit_percent,
        error_count=error_count,
        recent_errors=recent_errors,
        assistant_notes=assistant_notes,
        last_event_seq=last_event_seq,
        last_event_ts=last_event_ts,
        open_invoke_id=open_invoke_id,
        token_stats_error=token_stats_error,
    )


def get_child_thread_statuses(
    db: ThreadsDB,
    manager_thread_id: str,
    child_thread_ids: Optional[List[str]] = None,
    *,
    max_errors: int = 5,
) -> List[ChildThreadStatus]:
    """Return status records for selected descendants, or all direct children."""

    manager = (manager_thread_id or "").strip()
    if not manager:
        raise ValueError("manager_thread_id is required")
    if db.get_thread(manager) is None:
        raise ValueError(f"manager thread not found: {manager}")
    if child_thread_ids is None:
        targets = list_children_ids(db, manager)
    else:
        seen: set[str] = set()
        targets = []
        for raw in child_thread_ids:
            tid = str(raw or "").splitlines()[-1].strip()
            if tid and tid not in seen:
                seen.add(tid)
                targets.append(tid)
    return [get_child_thread_status(db, manager, tid, max_errors=max_errors) for tid in targets]


def enqueue_user_tool_call(
    db: ThreadsDB,
    thread_id: str,
    name: str,
    arguments: Any,
    *,
    content: Optional[str] = None,
    hidden: bool = True,
    keep_user_turn: bool = True,
    origin: str = "user_command",
    auto_approve: bool = True,
    approval_reason: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    tool_call_id: Optional[str] = None,
) -> str:
    """Enqueue a generic user-originated tool call (RA3).

    This is the common representation used by user commands and, later,
    REPL bridge calls: a ``msg.create`` with ``role='user'`` and a
    single OpenAI-style ``tool_calls`` entry, optionally followed by an
    automatic ``tool_call.approval`` event.
    """

    import json as _json

    tool_name = (name or "").strip()
    if not tool_name:
        raise ValueError("tool name is required")

    tc_id = tool_call_id or _ulid_like()
    if isinstance(arguments, str):
        args_json = arguments
    else:
        args_json = _json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)

    tool_call = {
        'id': tc_id,
        'type': 'function',
        'function': {
            'name': tool_name,
            'arguments': args_json,
        },
    }

    payload_extra: Dict[str, Any] = {
        'tool_calls': [tool_call],
        'keep_user_turn': bool(keep_user_turn),
        'origin': origin,
    }
    if hidden:
        payload_extra['no_api'] = True
    if extra:
        payload_extra.update(dict(extra))

    msg_content = content if content is not None else f"{origin}: {tool_name}"
    append_message(db, thread_id, 'user', msg_content, extra=payload_extra)

    if auto_approve:
        approve_tool_calls_for_thread(
            db,
            thread_id,
            decision='granted',
            reason=approval_reason or f'Auto-approved {origin} tool call',
            tool_call_id=tc_id,
        )

    return tc_id


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
    prefix = '$$ ' if hidden else '$ '
    return enqueue_user_tool_call(
        db,
        thread_id,
        'bash',
        {'script': script},
        content=f"{prefix}{script}",
        hidden=hidden,
        keep_user_turn=True,
        origin='user_command',
        auto_approve=True,
        approval_reason='Auto-approved as user-initiated bash command',
        extra={'user_command_type': '$$' if hidden else '$'},
    )


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


def _tool_call_result_now(db: ThreadsDB, thread_id: str, tool_call_id: str, *, timed_out: bool = False) -> ToolCallResult:
    """Build a ToolCallResult from current event-derived state."""

    from .tool_state import build_tool_call_states

    states = build_tool_call_states(db, thread_id)
    tc = states.get(tool_call_id)
    content = get_user_command_result(db, thread_id, tool_call_id)
    return ToolCallResult(
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        state=tc.state if tc is not None else "unknown",
        content=content,
        finished_reason=tc.finished_reason if tc is not None else None,
        output_decision=tc.output_decision if tc is not None else None,
        timed_out=timed_out,
    )


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _timeout_countdown_summary(
    prefix: str,
    timeout_sec: Optional[float],
    started_at: float,
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """Format timeout countdowns consistently for wait-style status events."""

    limit = _safe_float(timeout_sec)
    if limit is None or limit <= 0:
        return None
    current = time.time() if now is None else float(now)
    elapsed = max(0.0, current - float(started_at))
    remaining = max(0.0, limit - elapsed)
    return f"{prefix}; timeout in {remaining:.0f}s (limit {limit:.0f}s)"


def _append_tool_wait_summary(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    tool_name: str,
    timeout_sec: Optional[float],
    started_at: float,
    *,
    now: Optional[float] = None,
) -> None:
    summary = _timeout_countdown_summary(
        f"waiting for {tool_name or 'tool'} result",
        timeout_sec,
        started_at,
        now=now,
    )
    if not summary:
        return
    try:
        db.append_event(
            event_id=_ulid_like(),
            thread_id=thread_id,
            type_='tool_call.summary',
            msg_id=None,
            invoke_id=None,
            payload={
                'tool_call_id': tool_call_id,
                'name': tool_name or 'tool',
                'summary': summary,
            },
        )
    except Exception:
        pass


def wait_for_tool_call_result(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    timeout_sec: Optional[float] = 30.0,
    poll_interval: float = 0.1,
) -> ToolCallResult:
    """Wait for a specific tool call to reach TC6 and return details.

    The wait condition is derived entirely from persisted events, making
    it suitable as the event-log-backed "callback" used by REPL bridges.
    """

    from .tool_state import build_tool_call_states

    start = time.time()
    last_summary = 0.0
    while True:
        states = build_tool_call_states(db, thread_id)
        tc = states.get(tool_call_id)
        if tc is not None and tc.published:
            return _tool_call_result_now(db, thread_id, tool_call_id)
        limit = _safe_float(timeout_sec)
        if limit is not None and limit > 0 and tc is not None:
            now = time.time()
            if not last_summary or (now - last_summary) >= max(1.0, float(poll_interval)):
                last_summary = now
                _append_tool_wait_summary(db, thread_id, tool_call_id, tc.name or 'tool', limit, start, now=now)
        if limit is not None and (time.time() - start) >= limit:
            return _tool_call_result_now(db, thread_id, tool_call_id, timed_out=True)
        try:
            if limit is not None:
                remaining = max(0.0, limit - (time.time() - start))
                time.sleep(min(float(poll_interval), remaining))
            else:
                time.sleep(float(poll_interval))
        except Exception:
            time.sleep(0.1)


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
    result = wait_for_tool_call_result(
        db,
        thread_id,
        tool_call_id,
        timeout_sec=timeout_sec,
        poll_interval=poll_interval,
    )
    return result.content if not result.timed_out else None

async def wait_for_user_command_result_async(db: ThreadsDB, thread_id: str, tool_call_id: str,
                                             timeout_sec: float = 30.0, poll_interval: float = 0.1) -> Optional[str]:
    """Async version of wait_for_user_command_result."""
    result = await wait_for_tool_call_result_async(
        db,
        thread_id,
        tool_call_id,
        timeout_sec=timeout_sec,
        poll_interval=poll_interval,
    )
    return result.content if not result.timed_out else None


async def wait_for_tool_call_result_async(
    db: ThreadsDB,
    thread_id: str,
    tool_call_id: str,
    *,
    timeout_sec: Optional[float] = 30.0,
    poll_interval: float = 0.1,
) -> ToolCallResult:
    """Async event-log-backed wait for a specific tool call to publish."""

    import asyncio
    from .tool_state import build_tool_call_states
    loop = asyncio.get_running_loop()
    start = loop.time()
    last_summary = 0.0
    while True:
        states = build_tool_call_states(db, thread_id)
        tc = states.get(tool_call_id)
        if tc is not None and tc.published:
            return _tool_call_result_now(db, thread_id, tool_call_id)
        limit = _safe_float(timeout_sec)
        if limit is not None and limit > 0 and tc is not None:
            now = loop.time()
            if not last_summary or (now - last_summary) >= max(1.0, float(poll_interval)):
                last_summary = now
                _append_tool_wait_summary(db, thread_id, tool_call_id, tc.name or 'tool', limit, start, now=now)
        if limit is not None and (loop.time() - start) >= limit:
            return _tool_call_result_now(db, thread_id, tool_call_id, timed_out=True)
        if limit is not None:
            remaining = max(0.0, limit - (loop.time() - start))
            await asyncio.sleep(min(float(poll_interval), remaining))
        else:
            await asyncio.sleep(float(poll_interval))


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


def _last_assistant_content_from_snapshot(db: ThreadsDB, thread_id: str) -> str:
    """Return the last assistant message content for a thread.

    Prefer the snapshot cache when available, but fall back to the event log.
    Snapshots can lag immediately after a child thread finishes; the event log
    remains the source of truth for programmatic waits from REPL code.
    """

    row = db.get_thread(thread_id)
    if row and row.snapshot_json:
        try:
            snap = json.loads(row.snapshot_json)
        except Exception:
            snap = None
        if isinstance(snap, dict):
            msgs = snap.get('messages', []) or []
            for m in reversed(msgs):
                try:
                    if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
                        return m.get('content') or ''
                except Exception:
                    continue

    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq DESC",
            (thread_id,),
        )
    except Exception:
        return ''
    for (payload_json,) in cur.fetchall():
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            continue
        try:
            if payload.get('role') == 'assistant' and isinstance(payload.get('content'), str):
                return payload.get('content') or ''
        except Exception:
            continue
    return ''


def _clean_wait_thread_id(value: Any) -> str:
    """Normalize a wait target that may include surrounding tool-output text."""

    text = str(value or '').strip()
    if not text:
        return ''
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ''



def _wait_skipped_msg_ids(db: ThreadsDB, thread_id: str) -> set[str]:
    skipped: set[str] = set()
    try:
        cur = db.conn.execute(
            "SELECT msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.edit'",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    for msg_id, payload_json in rows:
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get('skipped_on_continue') and msg_id:
            skipped.add(str(msg_id))
    return skipped


def _is_llm_error_message(payload: Dict[str, Any]) -> bool:
    if payload.get('recovery_notice'):
        return False
    if payload.get('role') != 'system':
        return False
    content = payload.get('content')
    if not isinstance(content, str):
        return False
    low = content.lower()
    return 'llm/runner error' in low or 'llm error' in low or 'context limit exceeded' in low


def _latest_completed_llm_turn_seq(db: ThreadsDB, thread_id: str) -> int:
    """Return the last event_seq that represents an LLM turn result.

    Results are assistant messages (including tool-call-only assistants) or
    system messages that explicitly surface LLM/runner failure.  The event log,
    not the snapshot cache, is the source of truth for wait semantics.
    """

    skipped = _wait_skipped_msg_ids(db, thread_id)
    latest = -1
    llm_invokes: set[str] = set()
    try:
        cur = db.conn.execute(
            "SELECT event_seq, type, msg_id, invoke_id, payload_json FROM events "
            "WHERE thread_id=? AND type IN ('msg.create', 'stream.open', 'stream.delta', 'stream.close') "
            "ORDER BY event_seq ASC",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    for event_seq, type_, msg_id, invoke_id, payload_json in rows:
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if type_ == 'stream.open':
            if payload.get('stream_kind') == 'llm' and isinstance(invoke_id, str) and invoke_id:
                llm_invokes.add(invoke_id)
            continue
        if type_ == 'stream.delta':
            if (
                'text' in payload
                or 'reason' in payload
                or 'reasoning_summary' in payload
                or 'tool_call' in payload
            ) and isinstance(invoke_id, str) and invoke_id:
                llm_invokes.add(invoke_id)
            continue
        if type_ == 'stream.close':
            if isinstance(invoke_id, str) and invoke_id in llm_invokes:
                try:
                    latest = int(event_seq)
                except Exception:
                    pass
            continue
        if msg_id and str(msg_id) in skipped:
            continue
        role = payload.get('role')
        completed = False
        if role == 'assistant':
            completed = bool(
                payload.get('content')
                or payload.get('tool_calls')
                or payload.get('reasoning')
                or payload.get('reasoning_content')
                or payload.get('incomplete')
            )
        elif _is_llm_error_message(payload):
            completed = True
        if completed:
            try:
                latest = int(event_seq)
            except Exception:
                pass
    return latest


def _latest_api_trigger_seq(db: ThreadsDB, thread_id: str) -> int:
    """Return the last message event_seq that should trigger an LLM turn."""

    skipped = _wait_skipped_msg_ids(db, thread_id)
    latest = -1
    try:
        cur = db.conn.execute(
            "SELECT event_seq, msg_id, payload_json FROM events WHERE thread_id=? AND type='msg.create' ORDER BY event_seq ASC",
            (thread_id,),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    for event_seq, msg_id, payload_json in rows:
        if msg_id and str(msg_id) in skipped:
            continue
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if bool(payload.get('no_api')) or bool(payload.get('keep_user_turn')):
            continue
        role = payload.get('role')
        tool_calls = payload.get('tool_calls') or []
        triggers = (role == 'user' and not tool_calls) or role == 'tool'
        if triggers:
            try:
                latest = int(event_seq)
            except Exception:
                pass
    return latest


def _thread_wait_complete(db: ThreadsDB, thread_id: str) -> bool:
    """Deterministic event-log predicate for ``wait`` completion.

    A thread is complete for manager ``wait`` when there is no open stream, no
    unresolved tool call, no runner-actionable work, and every API-triggering
    user/tool message has a later LLM result (assistant or surfaced LLM error).
    This avoids treating timing gaps or polling timeouts as state.
    """

    try:
        # Treat expired leases as stale before asking whether a thread is still
        # running.  ``current_open`` returns rows regardless of expiry, so a
        # crashed runner could otherwise make wait block until its own timeout.
        row = db.current_open(thread_id)
        if row is not None:
            try:
                if str(row['lease_until']) <= _utcnow_iso():
                    db.release(thread_id, str(row['invoke_id']))
                else:
                    return False
            except Exception:
                return False
        if db.current_open(thread_id) is not None:
            return False
    except Exception:
        return False

    try:
        from .tool_state import build_tool_call_states, discover_runner_actionable_cached

        if any(tc.state != 'TC6' for tc in build_tool_call_states(db, thread_id).values()):
            return False
        if discover_runner_actionable_cached(db, thread_id) is not None:
            return False
    except Exception:
        return False

    latest_trigger = _latest_api_trigger_seq(db, thread_id)
    if latest_trigger < 0:
        return True
    latest_completion = _latest_completed_llm_turn_seq(db, thread_id)
    return latest_completion >= latest_trigger


def _thread_wait_state_after_cheap_checks(db: ThreadsDB, thread_id: str, row: ThreadRow) -> Optional[str]:
    """Return a known unfinished wait state, or None if reducer checks are needed."""

    if row.status == 'paused':
        return 'paused'

    try:
        open_row = db.current_open(thread_id)
    except Exception:
        open_row = None
    if open_row is None:
        return None

    try:
        if str(open_row['lease_until'] or '') <= _utcnow_iso():
            db.release(thread_id, str(open_row['invoke_id']))
            return None
        return 'running'
    except Exception:
        return 'running'


def _thread_wait_poll_once(
    db: ThreadsDB,
    thread_id: str,
    cached_unfinished: Optional[_ThreadWaitUnfinishedCache] = None,
    *,
    include_unfinished_details: bool = False,
) -> tuple[ThreadWaitResult, Optional[_ThreadWaitUnfinishedCache]]:
    """Evaluate one thread for ``wait_for_threads`` with cheap unchanged-state reuse."""

    row = db.get_thread(thread_id)
    if row is None:
        return ThreadWaitResult(
            thread_id=thread_id,
            finished=False,
            state='not_found',
        ), None

    cheap_state = _thread_wait_state_after_cheap_checks(db, thread_id, row)
    if cheap_state is not None:
        return ThreadWaitResult(
            thread_id=thread_id,
            finished=False,
            state=cheap_state,
            last_assistant_message=(
                _last_assistant_content_from_snapshot(db, thread_id)
                if include_unfinished_details else ''
            ),
            short_recap=row.short_recap,
        ), None

    event_watermark = db.max_event_seq(thread_id)
    if cached_unfinished is not None and cached_unfinished.event_watermark == event_watermark:
        return ThreadWaitResult(
            thread_id=thread_id,
            finished=False,
            state=cached_unfinished.state,
            last_assistant_message=(
                _last_assistant_content_from_snapshot(db, thread_id)
                if include_unfinished_details else ''
            ),
            short_recap=row.short_recap,
        ), cached_unfinished

    from .tool_state import thread_state

    try:
        state = thread_state(db, thread_id)
    except Exception:
        state = 'unknown'

    if state == 'waiting_user' and _thread_wait_complete(db, thread_id):
        return ThreadWaitResult(
            thread_id=thread_id,
            finished=True,
            state=state,
            last_assistant_message=_last_assistant_content_from_snapshot(db, thread_id),
            short_recap=row.short_recap,
        ), None

    # Only cache states that are deterministically unfinished without relying
    # on an exception path.  An ``unknown`` state should be rechecked each poll.
    result = ThreadWaitResult(
        thread_id=thread_id,
        finished=False,
        state=state,
        last_assistant_message=(
            _last_assistant_content_from_snapshot(db, thread_id)
            if include_unfinished_details else ''
        ),
        short_recap=row.short_recap,
    )
    if state in {'running', 'waiting_tool_approval', 'waiting_output_approval'}:
        return result, _ThreadWaitUnfinishedCache(event_watermark=event_watermark, state=state)
    return result, None

def wait_for_threads(
    db: ThreadsDB,
    thread_ids: List[str],
    *,
    timeout_sec: Optional[float] = None,
    poll_interval: float = 0.2,
) -> Dict[str, ThreadWaitResult]:
    """Wait for threads to finish and return structured results.

    Completion is a deterministic event-log predicate, not a timing guess: no
    open stream, no unresolved tool call, no runner-actionable work, and the
    latest API-triggering user/tool message (if any) has a later LLM result
    message.  ``timeout_sec`` only bounds how long this function blocks; it is
    not used to decide whether a thread is finished.
    """

    clean_ids = [_clean_wait_thread_id(t) for t in (thread_ids or []) if isinstance(t, (str, int))]
    clean_ids = [tid for tid in clean_ids if tid]
    start = time.time()
    finished: Dict[str, bool] = {tid: False for tid in clean_ids}
    results: Dict[str, ThreadWaitResult] = {}
    unfinished_cache: Dict[str, _ThreadWaitUnfinishedCache] = {}

    while True:
        all_done = True
        for tid in clean_ids:
            if finished.get(tid):
                continue
            result, cache_entry = _thread_wait_poll_once(db, tid, unfinished_cache.get(tid))
            if result.finished or result.state == 'not_found':
                results[tid] = result
                finished[tid] = True
                continue
            if cache_entry is not None:
                unfinished_cache[tid] = cache_entry
            else:
                unfinished_cache.pop(tid, None)
            all_done = False
        if all_done:
            break
        limit = _safe_float(timeout_sec)
        if limit is not None and (time.time() - start) >= limit:
            break
        try:
            if limit is not None:
                remaining = max(0.0, limit - (time.time() - start))
                time.sleep(min(float(poll_interval), remaining))
            else:
                time.sleep(float(poll_interval))
        except Exception:
            time.sleep(0.2)

    # Fill in unfinished entries with their current state.
    for tid in clean_ids:
        if tid in results:
            continue
        result, cache_entry = _thread_wait_poll_once(
            db,
            tid,
            unfinished_cache.get(tid),
            include_unfinished_details=True,
        )
        results[tid] = result
        if cache_entry is not None:
            unfinished_cache[tid] = cache_entry
    return results

def set_subtree_working_directory(db: ThreadsDB, root_thread_id: str, working_dir: str, reason: str = "user") -> None:
    """Apply working directory configuration to all threads in a subtree."""
    for tid in collect_subtree(db, root_thread_id):
        set_thread_working_directory(db, tid, working_dir, reason=reason)


# --------- Thread Scheduling API ---------------------------------------------

# Sentinel value for "unset" - to explicitly remove a previously set value
class _UnsetType:
    """Sentinel type for unsetting values."""
    def __repr__(self) -> str:
        return "UNSET"

UNSET = _UnsetType()


@dataclass
class ThreadSchedulingSettings:
    """Thread scheduling settings from thread.scheduling events."""
    priority: int = 0
    threshold: Optional[float] = None  # None = use global default
    api_timeout: Optional[float] = None  # None = use default (600s)


def get_thread_scheduling(db: ThreadsDB, thread_id: str) -> ThreadSchedulingSettings:
    """Get scheduling settings for a thread (from latest thread.scheduling event).

    Each event is self-contained - only fields present in the event are "set".
    Missing fields use defaults.
    """
    row = db.conn.execute(
        "SELECT payload_json FROM events WHERE thread_id=? AND type='thread.scheduling' "
        "ORDER BY event_seq DESC LIMIT 1",
        (thread_id,)
    ).fetchone()
    if row:
        payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else (row["payload_json"] or {})
        return ThreadSchedulingSettings(
            priority=payload.get("priority", 0),
            threshold=payload.get("threshold"),  # None if not in event
            api_timeout=payload.get("apiTimeout"),  # None if not in event
        )
    return ThreadSchedulingSettings()  # All defaults


def set_thread_scheduling(
    db: ThreadsDB,
    thread_id: str,
    priority=None,  # None = keep, UNSET = remove
    threshold=None,
    api_timeout=None,
) -> None:
    """Set thread scheduling settings. Creates a self-contained event.

    - None: Keep current value (from previous event)
    - UNSET: Explicitly remove/unset the field
    - value: Set to the given value

    The resulting event only contains explicitly set fields.
    """
    current = get_thread_scheduling(db, thread_id)
    payload: Dict[str, Any] = {}

    # Priority: always include (defaults to 0 if unset)
    if isinstance(priority, _UnsetType):
        pass  # Don't include in payload -> defaults to 0
    elif priority is not None:
        payload["priority"] = priority
    elif current.priority != 0:  # Keep if non-default
        payload["priority"] = current.priority

    # Threshold: optional
    if isinstance(threshold, _UnsetType):
        pass  # Don't include -> uses global default
    elif threshold is not None:
        payload["threshold"] = threshold
    elif current.threshold is not None:  # Keep if previously set
        payload["threshold"] = current.threshold

    # API timeout: optional
    if isinstance(api_timeout, _UnsetType):
        pass  # Don't include -> uses default 600s
    elif api_timeout is not None:
        payload["apiTimeout"] = api_timeout
    elif current.api_timeout is not None:  # Keep if previously set
        payload["apiTimeout"] = current.api_timeout

    db.append_event(
        event_id=_ulid_like(),
        thread_id=thread_id,
        type_='thread.scheduling',
        payload=payload
    )
