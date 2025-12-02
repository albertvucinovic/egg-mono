from __future__ import annotations

"""Per-thread tools configuration helpers.

This module centralises configuration that controls which tools are
visible to the LLM and which tools are allowed to execute in a given
thread. Configuration is stored as event-log entries so that it
automatically participates in eggthreads' event-sourced model.

Event type: ``tools.config``

Payload schema (all keys optional)::

    {
      "llm_tools_enabled": true | false,
      "disable": ["tool_name", ...],
      "enable":  ["tool_name", ...]
    }

Semantics:
  - ``llm_tools_enabled`` toggles whether RA1 presents any tools to the
    LLM in this thread. When False, RA1 passes ``tools=None`` to the
    provider and does not attempt tool-based responses.
  - ``disable`` / ``enable`` adjust a per-thread set of disabled tool
    *names*. Disabled tools are never exposed to the LLM and, when a
    tool call is attempted (RA2/RA3), they are treated as immediately
    finished with a synthetic "tool disabled" output instead of being
    executed.

Callers should use the helpers in this module rather than emitting
``tools.config`` events directly.
"""

from dataclasses import dataclass, field
from typing import Set, List

from .db import ThreadsDB


@dataclass
class ToolsConfig:
    """Represents the effective tools configuration for a thread.

    Attributes:
      - llm_tools_enabled: if False, RA1 will not expose tools to the
        LLM for this thread (``tools=None`` / ``tool_choice=None``).
      - disabled_tools: set of tool *names* (as used in ToolRegistry)
        that must not be exposed to the LLM and must not be executed
        when tool calls are processed (RA2/RA3).
      - has_explicit_config: True if at least one ``tools.config``
        event has been applied for this thread. This allows callers to
        decide whether to overlay model-level defaults when there is no
        explicit per-thread configuration.
    """

    llm_tools_enabled: bool = True
    disabled_tools: Set[str] = field(default_factory=set)
    has_explicit_config: bool = False


def get_thread_tools_config(db: ThreadsDB, thread_id: str) -> ToolsConfig:
    """Return the effective ToolsConfig for a thread.

    This walks ``tools.config`` events in order and applies their
    payloads to an initially permissive configuration.
    """

    cfg = ToolsConfig()
    try:
        cur = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tools.config' ORDER BY event_seq ASC",
            (thread_id,),
        )
    except Exception:
        return cfg

    import json as _json

    for (pj,) in cur.fetchall():
        try:
            payload = _json.loads(pj) if isinstance(pj, str) else (pj or {})
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            continue
        if "llm_tools_enabled" in payload:
            try:
                cfg.llm_tools_enabled = bool(payload["llm_tools_enabled"])
            except Exception:
                pass
            cfg.has_explicit_config = True
        # Apply incremental enables/disables for tool names
        disable = payload.get("disable") or []
        if isinstance(disable, (list, tuple)):
            for name in disable:
                if isinstance(name, str) and name.strip():
                    cfg.disabled_tools.add(name.strip())
                    cfg.has_explicit_config = True
        enable = payload.get("enable") or []
        if isinstance(enable, (list, tuple)):
            for name in enable:
                if isinstance(name, str) and name.strip():
                    cfg.disabled_tools.discard(name.strip())
                    cfg.has_explicit_config = True

    return cfg


def _append_tools_config_event(db: ThreadsDB, thread_id: str, payload: dict) -> None:
    """Internal helper: append a ``tools.config`` event with given payload."""

    import os as _os
    try:
        db.append_event(
            event_id=_os.urandom(10).hex(),
            thread_id=thread_id,
            type_="tools.config",
            msg_id=None,
            invoke_id=None,
            payload=payload,
        )
    except Exception:
        # Best-effort; configuration is advisory.
        pass


def set_thread_tools_enabled(db: ThreadsDB, thread_id: str, enabled: bool) -> None:
    """Enable or disable LLM tools for a thread (RA1 exposure).

    When ``enabled`` is False, RA1 will stop exposing tools to the LLM
    in this thread (``tools=None`` / ``tool_choice=None``), but
    user-initiated commands (RA3) can still be modelled as tool calls
    and executed locally according to per-tool disabled lists.
    """

    _append_tools_config_event(db, thread_id, {"llm_tools_enabled": bool(enabled)})


def disable_tool_for_thread(db: ThreadsDB, thread_id: str, name: str) -> None:
    """Mark a tool as disabled for this thread.

    Disabled tools are hidden from the LLM and, when a tool call is
    attempted (assistant- or user-originated), they are treated as
    immediately finished with a synthetic "tool disabled" output
    instead of being executed.
    """

    if not isinstance(name, str) or not name.strip():
        return
    _append_tools_config_event(db, thread_id, {"disable": [name.strip()]})


def enable_tool_for_thread(db: ThreadsDB, thread_id: str, name: str) -> None:
    """Remove a tool from the disabled set for this thread."""

    if not isinstance(name, str) or not name.strip():
        return
    _append_tools_config_event(db, thread_id, {"enable": [name.strip()]})


# -------- Subtree helpers -------------------------------------------------

def _collect_subtree(db: ThreadsDB, root_id: str) -> List[str]:
    """Return all thread_ids in the subtree rooted at ``root_id``.

    This is a simple BFS over the ``children`` table. The root itself
    is included as the first element of the returned list.
    """

    out: List[str] = []
    q: List[str] = [root_id]
    seen: Set[str] = set()
    while q:
        t = q.pop(0)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        try:
            cur = db.conn.execute("SELECT child_id FROM children WHERE parent_id=?", (t,))
            for (cid,) in cur.fetchall():
                if isinstance(cid, str):
                    q.append(cid)
        except Exception:
            continue
    return out


def set_subtree_tools_enabled(db: ThreadsDB, root_thread_id: str, enabled: bool) -> None:
    """Enable or disable LLM tools for all threads in a subtree.

    This is a convenience wrapper around :func:`set_thread_tools_enabled`
    that walks the subtree rooted at ``root_thread_id`` and appends a
    ``tools.config`` event for each thread.
    """

    for tid in _collect_subtree(db, root_thread_id):
        set_thread_tools_enabled(db, tid, enabled)


def disable_tool_for_subtree(db: ThreadsDB, root_thread_id: str, name: str) -> None:
    """Disable a tool for all threads in a subtree."""

    if not isinstance(name, str) or not name.strip():
        return
    for tid in _collect_subtree(db, root_thread_id):
        disable_tool_for_thread(db, tid, name)


def enable_tool_for_subtree(db: ThreadsDB, root_thread_id: str, name: str) -> None:
    """Enable a tool for all threads in a subtree."""

    if not isinstance(name, str) or not name.strip():
        return
    for tid in _collect_subtree(db, root_thread_id):
        enable_tool_for_thread(db, tid, name)

