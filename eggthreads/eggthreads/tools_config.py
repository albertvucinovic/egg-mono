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
      "allow_only": ["tool_name", ...] | null,
      "allow_raw_tool_output": true | false
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
    ``null`` clears the local allowlist. Disabled tools still win over
    allowlist entries.
  - ``allow_raw_tool_output`` defaults to False; it must be True at every
    level of a thread's ancestry before unmasked output reaches a provider.

Effective policy is the intersection of the thread and all ancestors. Any
policy DB/decode failure is distinguishable and fails closed. Callers should
use the helpers in this module rather than emitting
``tools.config`` events directly.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Set, List

from .db import ThreadsDB


class ToolPolicyReadError(RuntimeError):
    """Raised when a tool policy cannot be read or validated safely."""

    def __init__(self, kind: str, source_thread_id: str, detail: str):
        self.kind = kind
        self.source_thread_id = source_thread_id
        self.detail = detail
        super().__init__(f"{kind} for thread {source_thread_id}: {detail}")


@dataclass
class ToolsConfig:
    """Effective tool capability after intersecting the complete ancestry.

    Missing policy events use safe, usable defaults: tools are available but
    provider-bound tool output is secret-masked. ``has_explicit_config`` means
    any event exists on the effective ancestry. ``policy_error`` distinguishes a
    read/decode failure from that ordinary no-policy state. A config carrying
    an error is fail-closed for exposure, execution, and raw publication.
    """

    llm_tools_enabled: bool = True
    disabled_tools: Set[str] = field(default_factory=set)
    has_explicit_config: bool = False
    allow_raw_tool_output: bool = False
    allowed_tools: Optional[Set[str]] = None
    policy_error: Optional[str] = None
    policy_error_kind: Optional[str] = None
    policy_error_source_thread_id: Optional[str] = None

    @classmethod
    def fail_closed(cls, error: ToolPolicyReadError) -> "ToolsConfig":
        return cls(
            llm_tools_enabled=False,
            allow_raw_tool_output=False,
            allowed_tools=set(),
            policy_error=str(error),
            policy_error_kind=error.kind,
            policy_error_source_thread_id=error.source_thread_id,
        )

    def is_tool_allowed(self, name: str) -> bool:
        """Return whether *name* may be exposed or executed."""

        if self.policy_error or not isinstance(name, str) or not name.strip():
            return False
        key = name.strip().lower()
        if key in self.disabled_tools:
            return False
        return self.allowed_tools is None or key in self.allowed_tools


def _clean_tool_names(value: Any, *, field_name: str, thread_id: str) -> Set[str]:
    if not isinstance(value, (list, tuple)):
        raise ToolPolicyReadError(
            "invalid_payload",
            thread_id,
            f"{field_name} must be an array of non-empty tool names",
        )
    names: Set[str] = set()
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ToolPolicyReadError(
                "invalid_payload",
                thread_id,
                f"{field_name} contains an invalid tool name",
            )
        names.add(name.strip().lower())
    return names


def _read_local_tools_config(db: ThreadsDB, thread_id: str) -> ToolsConfig:
    """Read one thread's local events, raising on any ambiguous policy state."""

    import json as _json

    try:
        cursor = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tools.config' ORDER BY event_seq ASC",
            (thread_id,),
        )
        rows = cursor.fetchall()
    except Exception as exc:
        raise ToolPolicyReadError("db_read", thread_id, f"{type(exc).__name__}: {exc}") from exc

    cfg = ToolsConfig()
    for row in rows:
        try:
            raw_payload = row[0]
        except Exception as exc:
            raise ToolPolicyReadError("db_read", thread_id, f"unreadable tools.config row: {exc}") from exc
        try:
            payload = _json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except Exception as exc:
            raise ToolPolicyReadError("payload_decode", thread_id, f"invalid tools.config JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ToolPolicyReadError("invalid_payload", thread_id, "tools.config payload must be an object")

        recognized = False
        if "llm_tools_enabled" in payload:
            if not isinstance(payload["llm_tools_enabled"], bool):
                raise ToolPolicyReadError("invalid_payload", thread_id, "llm_tools_enabled must be boolean")
            cfg.llm_tools_enabled = payload["llm_tools_enabled"]
            recognized = True
        if "allow_raw_tool_output" in payload:
            if not isinstance(payload["allow_raw_tool_output"], bool):
                raise ToolPolicyReadError("invalid_payload", thread_id, "allow_raw_tool_output must be boolean")
            cfg.allow_raw_tool_output = payload["allow_raw_tool_output"]
            recognized = True
        if "allow_only" in payload:
            allow_only = payload["allow_only"]
            cfg.allowed_tools = None if allow_only is None else _clean_tool_names(
                allow_only,
                field_name="allow_only",
                thread_id=thread_id,
            )
            recognized = True
        if "disable" in payload:
            cfg.disabled_tools.update(
                _clean_tool_names(payload["disable"], field_name="disable", thread_id=thread_id)
            )
            recognized = True
        if "enable" in payload:
            cfg.disabled_tools.difference_update(
                _clean_tool_names(payload["enable"], field_name="enable", thread_id=thread_id)
            )
            recognized = True
        if recognized:
            cfg.has_explicit_config = True

    return cfg


def _thread_ancestry(db: ThreadsDB, thread_id: str) -> List[str]:
    """Return root-to-thread ancestry, rejecting missing/ambiguous/cyclic links."""

    chain: List[str] = []
    seen: Set[str] = set()
    current = thread_id
    while current:
        if current in seen:
            raise ToolPolicyReadError("ancestry_read", current, "cycle in child ancestry")
        seen.add(current)
        chain.append(current)
        try:
            exists = db.conn.execute(
                "SELECT 1 FROM threads WHERE thread_id=?",
                (current,),
            ).fetchone()
            parents = db.conn.execute(
                "SELECT parent_id FROM children WHERE child_id=?",
                (current,),
            ).fetchall()
        except Exception as exc:
            raise ToolPolicyReadError("ancestry_read", current, f"{type(exc).__name__}: {exc}") from exc
        if not exists:
            raise ToolPolicyReadError("ancestry_read", current, "thread does not exist")
        if len(parents) > 1:
            raise ToolPolicyReadError("ancestry_read", current, "thread has multiple parents")
        if parents and (not isinstance(parents[0][0], str) or not parents[0][0].strip()):
            raise ToolPolicyReadError("ancestry_read", current, "parent id is invalid")
        current = parents[0][0].strip() if parents else ""
    chain.reverse()
    return chain


def _intersect_allowed(
    current: Optional[Set[str]],
    restriction: Optional[Set[str]],
) -> Optional[Set[str]]:
    if current is None:
        return None if restriction is None else set(restriction)
    if restriction is None:
        return current
    return current.intersection(restriction)


def _emit_policy_error_diagnostic(db: ThreadsDB, target_thread_id: str, error: ToolPolicyReadError) -> None:
    """Best-effort durable diagnostic for an already fail-closed read result."""

    import json as _json
    import os as _os

    payload = {
        "operation": "read_tools_config",
        "error_kind": error.kind,
        "source_thread_id": error.source_thread_id,
        "detail": error.detail,
        "fail_closed": True,
    }
    try:
        row = db.conn.execute(
            "SELECT payload_json FROM events WHERE thread_id=? AND type='tools.policy_error' ORDER BY event_seq DESC LIMIT 1",
            (target_thread_id,),
        ).fetchone()
        if row:
            previous = _json.loads(row[0])
            if isinstance(previous, dict) and all(previous.get(key) == value for key, value in payload.items()):
                return
    except Exception:
        pass
    try:
        db.append_event(
            event_id=_os.urandom(10).hex(),
            thread_id=target_thread_id,
            type_="tools.policy_error",
            msg_id=None,
            invoke_id=None,
            payload=payload,
        )
    except Exception:
        # The typed error remains inspectable on the returned config even when
        # the same DB fault prevents durable diagnostics.
        pass


def get_thread_tools_config(db: ThreadsDB, thread_id: str) -> ToolsConfig:
    """Return fail-closed effective policy intersected across all ancestors.

    A missing ``tools.config`` event is not an error and retains usable defaults.
    Any ancestry, database, or payload failure returns a distinguishable
    fail-closed config and attempts to append a ``tools.policy_error`` event.
    """

    try:
        ancestry = _thread_ancestry(db, thread_id)
        # Intersection identities. Each thread's local no-policy default still
        # has allow_raw_tool_output=False, so a policy-free root stays masked.
        effective = ToolsConfig(allow_raw_tool_output=True)
        for source_thread_id in ancestry:
            local = _read_local_tools_config(db, source_thread_id)
            effective.llm_tools_enabled = effective.llm_tools_enabled and local.llm_tools_enabled
            effective.allow_raw_tool_output = effective.allow_raw_tool_output and local.allow_raw_tool_output
            # A local "enable" only edits that local reducer. Unioning each
            # final local disabled set means it can never erase an ancestor deny.
            effective.disabled_tools.update(local.disabled_tools)
            effective.allowed_tools = _intersect_allowed(effective.allowed_tools, local.allowed_tools)
            effective.has_explicit_config = effective.has_explicit_config or local.has_explicit_config
        return effective
    except ToolPolicyReadError as error:
        _emit_policy_error_diagnostic(db, thread_id, error)
        return ToolsConfig.fail_closed(error)


def _append_tools_config_event(db: ThreadsDB, thread_id: str, payload: dict) -> None:
    """Append a policy event; configuration writes are never advisory."""

    import os as _os

    db.append_event(
        event_id=_os.urandom(10).hex(),
        thread_id=thread_id,
        type_="tools.config",
        msg_id=None,
        invoke_id=None,
        payload=payload,
    )


def set_thread_tools_enabled(db: ThreadsDB, thread_id: str, enabled: bool) -> None:
    """Set whether this thread may expose tools to its LLM."""

    _append_tools_config_event(db, thread_id, {"llm_tools_enabled": bool(enabled)})


def set_thread_allow_raw_tool_output(db: ThreadsDB, thread_id: str, allow: bool) -> None:
    """Set raw provider publication; effective ancestors may still deny it."""

    _append_tools_config_event(db, thread_id, {"allow_raw_tool_output": bool(allow)})


def disable_tool_for_thread(db: ThreadsDB, thread_id: str, name: str) -> None:
    """Disable a tool locally; ancestor restrictions also remain effective."""

    if not isinstance(name, str) or not name.strip():
        return
    _append_tools_config_event(db, thread_id, {"disable": [name.strip()]})


def enable_tool_for_thread(db: ThreadsDB, thread_id: str, name: str) -> None:
    """Remove a local disable without overriding an ancestor restriction."""

    if not isinstance(name, str) or not name.strip():
        return
    _append_tools_config_event(db, thread_id, {"enable": [name.strip()]})


def set_thread_tool_allowlist(db: ThreadsDB, thread_id: str, names: List[str] | Set[str]) -> None:
    """Set a local allowlist, intersected with all effective ancestors."""

    if not isinstance(names, (list, tuple, set)):
        names = []
    clean = sorted({n.strip() for n in names if isinstance(n, str) and n.strip()})
    _append_tools_config_event(db, thread_id, {"allow_only": clean})


def clear_thread_tool_allowlist(db: ThreadsDB, thread_id: str) -> None:
    """Clear the local allowlist without clearing ancestor restrictions."""

    _append_tools_config_event(db, thread_id, {"allow_only": None})


def inherited_tools_config_payload(db: ThreadsDB, parent_thread_id: str) -> dict[str, Any]:
    """Build the mandatory policy payload for an ordinary new child."""

    cfg = get_thread_tools_config(db, parent_thread_id)
    if cfg.policy_error:
        raise ToolPolicyReadError(
            cfg.policy_error_kind or "policy_read",
            cfg.policy_error_source_thread_id or parent_thread_id,
            cfg.policy_error,
        )
    return {
        "llm_tools_enabled": cfg.llm_tools_enabled,
        "allow_raw_tool_output": cfg.allow_raw_tool_output,
        "allow_only": None if cfg.allowed_tools is None else sorted(cfg.allowed_tools),
        "disable": sorted(cfg.disabled_tools),
    }


def inherit_tools_config_for_child(db: ThreadsDB, parent_thread_id: str, child_thread_id: str) -> None:
    """Persist a child's initial effective policy, raising on any failure."""

    _append_tools_config_event(
        db,
        child_thread_id,
        inherited_tools_config_payload(db, parent_thread_id),
    )


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
      - ``status``: one of ``"enabled"``, ``"disabled"``, ``"not_allowed"``,
        or ``"policy_error"``
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
        policy_error = bool(getattr(cfg, "policy_error", None))
        enabled = bool(key) and allowed_by_allowlist and not is_disabled and not policy_error

        if policy_error:
            status = "policy_error"
            status_label = "POLICY ERROR"
        elif is_disabled:
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

