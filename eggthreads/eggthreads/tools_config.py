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
      "enable":  ["tool_name", ...],
      "allow_only": ["tool_name", ...] | null
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
  - ``allow_only`` restricts the thread to exactly those tool names.
    ``null`` clears the allowlist. Disabled tools still win over
    allowlist entries.

Callers should use the helpers in this module rather than emitting
``tools.config`` events directly.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Set, List

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
      - allow_raw_tool_output: when False (default), tool outputs are
        *masked for secrets* when constructing the provider API request
        (see ThreadRunner._sanitize_messages_for_api). When True, tool
        outputs are sent to the provider as-is (still with control-char
        sanitization for safety).

        This flag does not prevent tool outputs from being stored in the
        local database or shown in the local UI; its primary purpose is
        to prevent accidental secret leakage to the LLM provider.
    """

    llm_tools_enabled: bool = True
    disabled_tools: Set[str] = field(default_factory=set)
    has_explicit_config: bool = False
    allow_raw_tool_output: bool = True
    allowed_tools: Optional[Set[str]] = None

    def is_tool_allowed(self, name: str) -> bool:
        """Return True if *name* may be exposed/executed in this thread.

        ``allowed_tools=None`` means "all registered tools are potentially
        allowed".  ``disabled_tools`` is always applied last so an explicit
        disable wins over an allowlist entry.
        """

        if not isinstance(name, str) or not name.strip():
            return False
        key = name.strip().lower()
        disabled = {n.lower() for n in self.disabled_tools}
        if key in disabled:
            return False
        if self.allowed_tools is None:
            return True
        allowed = {n.lower() for n in self.allowed_tools}
        return key in allowed


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
        if "allow_raw_tool_output" in payload:
            try:
                cfg.allow_raw_tool_output = bool(payload["allow_raw_tool_output"])
            except Exception:
                pass
            cfg.has_explicit_config = True
        if "allow_only" in payload:
            allow_only = payload.get("allow_only")
            if allow_only is None:
                cfg.allowed_tools = None
                cfg.has_explicit_config = True
            elif isinstance(allow_only, (list, tuple)):
                allowed: Set[str] = set()
                for name in allow_only:
                    if isinstance(name, str) and name.strip():
                        allowed.add(name.strip())
                cfg.allowed_tools = allowed
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


def set_thread_allow_raw_tool_output(db: ThreadsDB, thread_id: str, allow: bool) -> None:
    """Enable or disable raw (unfiltered) tool output for a thread.

    When ``allow`` is False (default), tool outputs are masked for
    secret-like values when constructing provider API messages.

    When True, tool outputs are sent to the provider without secret
    masking (but still with control-character sanitization).
    """

    _append_tools_config_event(db, thread_id, {"allow_raw_tool_output": bool(allow)})


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


def set_thread_tool_allowlist(db: ThreadsDB, thread_id: str, names: List[str] | Set[str]) -> None:
    """Restrict a thread to exactly the given tool names.

    The allowlist controls both LLM tool exposure and RA2/RA3 execution.
    Existing disabled tools remain disabled; i.e. disables still override
    allowlist membership.
    """

    if not isinstance(names, (list, tuple, set)):
        names = []
    clean = sorted({n.strip() for n in names if isinstance(n, str) and n.strip()})
    _append_tools_config_event(db, thread_id, {"allow_only": clean})


def clear_thread_tool_allowlist(db: ThreadsDB, thread_id: str) -> None:
    """Clear any explicit allowlist, returning to all-tools-minus-disabled."""

    _append_tools_config_event(db, thread_id, {"allow_only": None})


def inherit_tools_config_for_child(db: ThreadsDB, parent_thread_id: str, child_thread_id: str) -> None:
    """Copy the parent's effective tools config onto a newly-created child.

    Tool configuration is a capability boundary, so descendants should start
    with the parent's current restrictions.  We intentionally copy by value at
    creation time instead of resolving through ancestors dynamically: later
    parent changes do not silently mutate existing children, while trusted
    programmatic code can still widen a child explicitly with the normal
    ``set_thread_tool_allowlist`` / ``clear_thread_tool_allowlist`` /
    ``enable_tool_for_thread`` helpers.
    """

    cfg = get_thread_tools_config(db, parent_thread_id)
    if not cfg.has_explicit_config:
        return

    payload: dict[str, Any] = {
        "llm_tools_enabled": bool(cfg.llm_tools_enabled),
        "allow_raw_tool_output": bool(cfg.allow_raw_tool_output),
    }
    if cfg.allowed_tools is not None:
        payload["allow_only"] = sorted(cfg.allowed_tools)
    if cfg.disabled_tools:
        payload["disable"] = sorted(cfg.disabled_tools)

    _append_tools_config_event(db, child_thread_id, payload)


def get_tool_statuses_for_config(
    cfg: ToolsConfig,
    available_tools: Mapping[str, Mapping[str, Any]],
) -> List[dict[str, Any]]:
    """Return effective per-tool statuses for a tools configuration.

    ``/toolsStatus`` callers need the same capability decision as RA1/RA3:
    a tool is usable only when it is in the explicit allowlist (if one is
    configured) and is not disabled.  This helper centralises that logic so
    status UIs do not accidentally report allowlist-excluded tools as enabled.

    Each returned dict contains:
      - ``name``: registered tool name
      - ``enabled``: effective usability in this thread
      - ``status``: one of ``"enabled"``, ``"disabled"``, ``"not_allowed"``
      - ``status_label``: human-readable label for command output
      - ``disabled``: whether the tool is explicitly disabled
      - ``allowed_by_allowlist``: whether the allowlist permits the tool
      - ``local_only``: registry metadata, if present
    """

    disabled_set = {
        n.strip().lower()
        for n in (getattr(cfg, "disabled_tools", None) or set())
        if isinstance(n, str) and n.strip()
    }

    allowed_raw = getattr(cfg, "allowed_tools", None)
    allowed_set: Optional[Set[str]]
    if allowed_raw is None:
        allowed_set = None
    else:
        allowed_set = {
            n.strip().lower()
            for n in allowed_raw
            if isinstance(n, str) and n.strip()
        }

    statuses: List[dict[str, Any]] = []
    for name, info in sorted(available_tools.items()):
        key = name.strip().lower() if isinstance(name, str) else ""
        is_disabled = key in disabled_set
        allowed_by_allowlist = allowed_set is None or key in allowed_set
        enabled = bool(key) and allowed_by_allowlist and not is_disabled

        if is_disabled:
            status = "disabled"
            status_label = "DISABLED"
        elif not allowed_by_allowlist:
            status = "not_allowed"
            status_label = "not allowed"
        else:
            status = "enabled"
            status_label = "enabled"

        statuses.append({
            "name": name,
            "enabled": enabled,
            "status": status,
            "status_label": status_label,
            "disabled": is_disabled,
            "allowed_by_allowlist": allowed_by_allowlist,
            "local_only": bool((info or {}).get("local_only", False)),
        })

    return statuses


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

