from __future__ import annotations

"""Sandbox helpers for tool execution.

This module centralises integration with the
``@anthropic-ai/sandbox-runtime`` CLI (``srt``).

Goals
-----

* Provide a **single place** where eggthreads decides whether tool
  executions (bash, python, etc.) should be wrapped in the sandbox.
* Keep a **default** configuration per working directory (the directory
  from which the process is started), under ``.egg/srt/default.json``.

* Support **per-thread** sandbox configuration via DB events:

  - A thread may have a ``sandbox.config`` event whose payload contains
    the **full sandbox settings JSON**.
  - If a thread has no config event, it inherits the nearest ancestor's
    config event.
  - If neither the thread nor any ancestor has a config event, the
    default settings file ``.egg/srt/default.json`` is used.

  There is intentionally **no process-wide** sandbox configuration.

Key concepts
------------

* The sandbox is considered **available** if an ``srt`` binary can be
  resolved.  The binary path can be overridden via ``EGG_SRT_BIN``.

* Effective behaviour (per tool invocation):

  - If the *effective thread config* has ``enabled=True`` *and* ``srt``
    is available → tool commands are wrapped as
    ``srt --settings <effective-config> <command>``.
  - Otherwise the original argv is returned unchanged and callers run
    tools directly (unsandboxed).

* Configuration files live under ``.egg/srt/`` in the current working
  directory.  We create a default configuration file
  ``.egg/srt/default.json`` which is intentionally conservative:

    - On the filesystem, only the current directory is allowed for
      reading and writing.

* Optional *named* configuration files may exist under ``.egg/srt/``
  (e.g. ``.egg/srt/readall.json``). UIs may load such a file and store
  the full JSON into a ``sandbox.config`` event.

* Regardless of which configuration is used, **all files under
  ``.egg/srt/`` are always off limits for writing inside the sandbox**.
  In particular, ``.egg/srt/default.json`` must never be writable from
  within the sandbox. We enforce this by augmenting every effective
  config with mandatory ``filesystem.denyWrite`` entries.

Public API
----------

* :func:`wrap_argv_for_sandbox_with_settings(argv, enabled, settings)` –
  prepend ``srt --settings ...`` when sandboxing is effective.
* :func:`get_thread_sandbox_config(db, thread_id)` – resolve the
  effective per-thread configuration (including ancestor inheritance).
* :func:`set_thread_sandbox_config(db, thread_id, ...)` – persist a
  per-thread configuration as an event containing the full JSON.
* :func:`get_thread_sandbox_status(db, thread_id)` – status dict for UIs.

The implementation is deliberately self‑contained and avoids importing
other eggthreads modules to prevent circular imports.
"""

from dataclasses import dataclass
import json
import os
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

import shutil


if TYPE_CHECKING:  # pragma: no cover
    from .db import ThreadsDB


def _srt_dir() -> Path:
    """Return the per-working-directory settings dir (``.egg/srt``).

    We intentionally compute this dynamically from :func:`Path.cwd`
    instead of capturing the CWD at import time. This keeps the module
    robust in test suites (which often chdir) and in interactive usage
    where callers may change directories.
    """

    return (Path.cwd() / ".egg" / "srt").resolve()


def _ensure_srt_dir() -> Path:
    """Ensure ``.egg/srt`` exists and return its path."""

    srt_dir = _srt_dir()
    try:
        srt_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Best-effort; callers may still try to write configs and fail.
        pass
    return srt_dir


def _default_config_dict() -> Dict[str, object]:
    """Return the in‑memory default sandbox configuration.

    The default is intentionally simple: it restricts filesystem access
    to the current working directory but leaves network access
    unrestricted ("allow all domains") so that existing tool usage
    with outbound HTTP continues to work without additional
    configuration.  Users can provide stricter configs under
    ``.egg/srt/`` and select them via :func:`set_srt_sandbox_configuration`.
    """

    # NOTE: sandbox-runtime currently supports a deny-only model for
    # reads (``filesystem.denyRead``). That means we cannot express
    # "allow reads only under ." in a portable way. We *can* express
    # "allow writes only under ." (``filesystem.allowWrite``).

    return {
        # Secure-by-default network: empty allowlist means "deny all".
        "network": {
            "allowedDomains": [],
            "deniedDomains": [],
        },
        "filesystem": {
            # Read restrictions are deny-only; we keep this empty by
            # default. Users may add explicit denies for sensitive
            # paths.
            "denyRead": [],
            # Write restrictions are allow-only.
            "allowWrite": ["."],
            # Denies will be extended at runtime to always protect our
            # settings directory and default.json.
            "denyWrite": [".egg/srt"],
        },
    }


def _default_config_path() -> Path:
    """Return the path to the default config, creating it if needed."""

    cfg_dir = _ensure_srt_dir()
    path = cfg_dir / "default.json"
    if not path.exists():
        try:
            path.write_text(json.dumps(_default_config_dict(), indent=2), encoding="utf-8")
        except Exception:
            # If writing fails we still return the path; callers will
            # fall back to an in‑memory default when loading.
            pass
    return path


# Global enable flag for this process.  Sandboxing starts out enabled
# and can be toggled programmatically via :func:`set_sandbox_globally_enabled`
# (for example from a UI command).  This replaces the previous
# ``EGG_SANDBOX_MODE`` environment variable so that configuration is
# explicit in application logic instead of being hidden in the
# environment.
_DEFAULT_ENABLED: bool = True

_SRT_BIN = (os.environ.get("EGG_SRT_BIN") or "srt").strip() or "srt"


def _normalize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "default.json"
    # Prevent directory traversal outside .egg/srt – treat name as a
    # simple file name.
    name = os.path.basename(name)
    if not name.endswith(".json"):
        name += ".json"
    return name


def _config_source_path(name: Optional[str] = None) -> Path:
    """Return the *source* config path for a given name.

    If the named file does not exist, the default config path is
    returned instead.
    """

    cfg_dir = _ensure_srt_dir()
    if not name:
        return _default_config_path()
    norm = _normalize_name(name)
    path = cfg_dir / norm
    if path.exists():
        return path
    return _default_config_path()


def _load_config(path: Path) -> Dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Fallback to in‑memory default if file is missing or invalid
    return _default_config_dict()


def _effective_config_path(config_name: Optional[str] = None) -> Path:
    """Backward compatible helper.

    We no longer have a process-wide active config. This function is
    retained so older callers that expect a per-file effective config
    can keep working.
    """

    try:
        src_path = _config_source_path(_normalize_name(config_name or "default.json"))
        cfg = _load_config(src_path)
    except Exception:
        cfg = _load_config(_default_config_path())
    return _effective_config_path_from_settings(cfg)


def sandbox_enabled() -> bool:
    """Return the default sandbox-enabled policy.

    There is no process-wide sandbox *configuration*; however, Egg
    historically supported disabling sandboxing globally via
    ``EGG_SANDBOX_MODE``. We keep this as an emergency escape hatch.

    Per-thread config can override this default via ``sandbox.config``
    events.
    """

    try:
        mode = str(os.environ.get("EGG_SANDBOX_MODE") or "").strip().lower()
        if mode in ("0", "off", "false", "no"):
            return False
        if mode in ("1", "on", "true", "yes"):
            return True
    except Exception:
        pass
    return _DEFAULT_ENABLED


def sandbox_available() -> bool:
    """Return whether an ``srt`` binary is available."""
    try:
        return shutil.which(_SRT_BIN) is not None
    except Exception:
        return False


def set_sandbox_globally_enabled(enabled: bool) -> None:
    """Backward-compatible no-op.

    Egg previously had a process-wide sandbox toggle. The current
    architecture is thread/event based.

    We keep this function so existing callers do not break, but it only
    changes the default enable policy for threads *created in this
    process* that do not have an explicit (or inherited) sandbox config.
    """

    global _DEFAULT_ENABLED
    _DEFAULT_ENABLED = bool(enabled)


def set_sandbox_config(*, enabled: bool, config_name: Optional[str] = None) -> None:
    """Backward-compatible helper.

    This does **not** implement a process-wide configuration anymore.
    It only:

      * sets the default enabled policy for the process, and
      * validates that the named config exists under .egg/srt.

    Callers that want a thread to actually use the config must store it
    into the thread via :func:`set_thread_sandbox_config`.
    """

    set_sandbox_globally_enabled(bool(enabled))
    if isinstance(config_name, str) and config_name.strip():
        norm = _normalize_name(config_name)
        cfg_dir = _ensure_srt_dir()
        path = cfg_dir / norm
        if not path.exists():
            raise ValueError(f"srt configuration file not found: {path}")


def set_srt_sandbox_configuration(name: str) -> None:
    """Backward compatible alias."""

    set_sandbox_config(enabled=sandbox_enabled(), config_name=name)


def is_sandbox_effective() -> bool:
    """Return True if tool commands will actually be sandboxed."""
    return sandbox_enabled() and sandbox_available()


def wrap_argv_for_sandbox(argv: List[str]) -> List[str]:
    """Backward-compatible convenience wrapper.

    If called without thread context, we use the default settings
    (``.egg/srt/default.json``) and the default enabled policy.
    """

    return wrap_argv_for_sandbox_with_settings(argv, enabled=sandbox_enabled(), settings=_load_config(_default_config_path()))


def wrap_argv_for_sandbox_with_settings(
    argv: List[str],
    *,
    enabled: bool,
    settings: Dict[str, object],
) -> List[str]:
    """Wrap an argv for sandbox execution with explicit settings.

    ``srt`` expects a single command string (it executes via a
    shell-like layer). When sandboxing is effective, we:

      1) write the effective settings file (augmented with mandatory
         protections), and
      2) pass a shell-escaped command string built from ``argv``.
    """

    if not (bool(enabled) and sandbox_available()):
        return argv

    eff_path = _effective_config_path_from_settings(settings)

    try:
        import shlex

        cmd_str = shlex.join(list(argv))
    except Exception:
        cmd_str = " ".join(str(x) for x in argv)

    return [_SRT_BIN, "--settings", str(eff_path), cmd_str]


def wrap_bash_argv_for_sandbox(argv: List[str], eff_path) -> List[str]:  # pragma: no cover
    """Backward-compatible wrapper.

    Retained for callers that still import it.
    """

    return [_SRT_BIN, "--settings", str(eff_path), " ".join(argv)]


def wrap_argv_for_sandbox_with_config(
    argv: List[str],
    *,
    enabled: Optional[bool],
    config_name: Optional[str],
) -> List[str]:
    """Backward compatible wrapper used by older callers.

    We now store full settings JSON in thread events. This helper still
    accepts a config name, loads that file (or default.json) and
    delegates to :func:`wrap_argv_for_sandbox_with_settings`.
    """

    eff_enabled = sandbox_enabled() if enabled is None else bool(enabled)
    try:
        p = _config_source_path(_normalize_name(config_name or "default.json"))
        settings = _load_config(p)
    except Exception:
        settings = _load_config(_default_config_path())
    return wrap_argv_for_sandbox_with_settings(argv, enabled=eff_enabled, settings=settings)


@dataclass
class ThreadSandboxConfig:
    """Effective sandbox selection for a thread."""

    enabled: bool
    settings: Dict[str, object]
    source: str


def _parent_id(db: "ThreadsDB", thread_id: str) -> Optional[str]:
    try:
        row = db.conn.execute(
            "SELECT parent_id FROM children WHERE child_id=? LIMIT 1",
            (thread_id,),
        ).fetchone()
        if row and isinstance(row[0], str) and row[0]:
            return row[0]
    except Exception:
        pass
    return None


def _nearest_sandbox_event_payload(db: "ThreadsDB", thread_id: str) -> Optional[Dict[str, object]]:
    """Return the nearest ancestor's sandbox.config payload (including self)."""

    tid: Optional[str] = thread_id
    seen: set[str] = set()
    while tid and tid not in seen:
        seen.add(tid)
        try:
            row = db.conn.execute(
                "SELECT payload_json FROM events WHERE thread_id=? AND type='sandbox.config' ORDER BY event_seq DESC LIMIT 1",
                (tid,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            try:
                payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                return payload  # may be legacy (config_name-only)
        tid = _parent_id(db, tid)
    return None


def get_thread_sandbox_config(db: "ThreadsDB", thread_id: str) -> ThreadSandboxConfig:
    """Return the effective sandbox config for a thread.

    Resolution order:

    1) Latest ``sandbox.config`` event on the thread.
    2) Latest ``sandbox.config`` event on the nearest ancestor.
    3) ``.egg/srt/default.json`` (created if missing).

    The returned config contains the full settings dict. Mandatory
    protections (e.g. denying writes to ``.egg/srt``) are applied at
    execution time when we write the *effective* settings file.
    """

    enabled = sandbox_enabled()
    settings: Dict[str, object] = _load_config(_default_config_path())
    source = "default.json"

    payload = _nearest_sandbox_event_payload(db, thread_id)
    if isinstance(payload, dict) and payload:
        # Enabled flag
        if "enabled" in payload:
            try:
                enabled = bool(payload.get("enabled"))
            except Exception:
                pass

        # Preferred modern format: payload contains full settings JSON.
        cfg = payload.get("settings") or payload.get("config")
        if isinstance(cfg, dict) and cfg:
            settings = cfg  # type: ignore[assignment]
            source = str(payload.get("source") or payload.get("config_source") or "event")
        else:
            # Backward compatibility: payload only has a config_name.
            nm = payload.get("config_name")
            if isinstance(nm, str) and nm.strip():
                try:
                    p = _config_source_path(_normalize_name(nm))
                    settings = _load_config(p)
                    source = f"file:{_normalize_name(nm)}"
                except Exception:
                    pass

    return ThreadSandboxConfig(enabled=bool(enabled), settings=dict(settings), source=str(source))


def set_thread_sandbox_config(
    db: "ThreadsDB",
    thread_id: str,
    *,
    enabled: bool,
    config_name: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    reason: str = "user",
) -> None:
    """Persist sandbox configuration for a thread.

    This appends a ``sandbox.config`` event so that the effective
    sandbox choice is reproducible across processes.
    """

    import os as _os

    payload: Dict[str, object] = {
        "enabled": bool(enabled),
        "reason": reason,
    }

    # Determine which settings JSON to persist.
    src_name: str = ""

    if isinstance(settings, dict) and settings:
        payload["settings"] = settings
        src_name = "inline"
    else:
        # If a config file name was provided, load it and persist the
        # full JSON in the event.
        if isinstance(config_name, str) and config_name.strip():
            norm = _normalize_name(config_name)
            cfg_dir = _ensure_srt_dir()
            path = cfg_dir / norm
            if not path.exists():
                raise ValueError(f"srt configuration file not found: {path}")
            payload["settings"] = _load_config(path)
            payload["source"] = f"file:{norm}"
            src_name = norm
        else:
            # Default
            payload["settings"] = _load_config(_default_config_path())
            payload["source"] = "default.json"
            src_name = "default.json"

    if src_name and "source" not in payload:
        payload["source"] = src_name

    try:
        db.append_event(
            event_id=_os.urandom(10).hex(),
            thread_id=thread_id,
            type_="sandbox.config",
            msg_id=None,
            invoke_id=None,
            payload=payload,
        )
    except Exception:
        pass


def get_thread_sandbox_status(db: "ThreadsDB", thread_id: str) -> Dict[str, object]:
    """Return sandbox status for a specific thread.

    This mirrors :func:`get_sandbox_status` but derives the configured
    enabled/settings values from the thread's inherited
    ``sandbox.config`` event.
    """

    cfg = get_thread_sandbox_config(db, thread_id)
    effective = bool(cfg.enabled) and sandbox_available()
    warning: Optional[str] = None
    if bool(cfg.enabled) and not sandbox_available():
        warning = (
            "Sandboxing is enabled but the 'srt' CLI was not found. "
            "Tool commands will run *without* a sandbox. Install it "
            "with: npm install -g @anthropic-ai/sandbox-runtime."
        )

    # Best-effort: source_path is only meaningful for default.json or
    # file:* sources.
    try:
        if isinstance(cfg.source, str) and cfg.source.startswith("file:"):
            nm = cfg.source.split(":", 1)[1]
            src = _config_source_path(_normalize_name(nm))
        else:
            src = _default_config_path()
    except Exception:
        src = _default_config_path()

    return {
        "enabled": bool(cfg.enabled),
        "available": sandbox_available(),
        "effective": effective,
        "mode": "on" if bool(cfg.enabled) else "off",
        "srt_bin": _SRT_BIN,
        "config_source": cfg.source,
        "config_path": str(src),
        "settings_dir": str(_ensure_srt_dir()),
        "warning": warning,
    }


def set_subtree_sandbox_config(
    db: "ThreadsDB",
    root_thread_id: str,
    *,
    enabled: bool,
    config_name: Optional[str] = None,
    reason: str = "user",
) -> None:
    """Apply sandbox configuration to all threads in a subtree."""

    # Local BFS to avoid importing other modules (and potential cycles).
    q: List[str] = [root_thread_id]
    seen: set[str] = set()
    while q:
        tid = q.pop(0)
        if tid in seen:
            continue
        seen.add(tid)
        set_thread_sandbox_config(
            db,
            tid,
            enabled=enabled,
            config_name=config_name,
            reason=reason,
        )
        try:
            cur = db.conn.execute(
                "SELECT child_id FROM children WHERE parent_id=? ORDER BY child_id",
                (tid,),
            )
            for (cid,) in cur.fetchall():
                if isinstance(cid, str) and cid:
                    q.append(cid)
        except Exception:
            continue

@dataclass
class SrtSandboxConfiguration:
    """Metadata about the per-working-directory sandbox settings."""

    settings_dir: str
    default_path: str


def get_srt_sandbox_configuration() -> SrtSandboxConfiguration:
    """Return metadata for the working-directory settings folder."""

    cfg_dir = _ensure_srt_dir()
    return SrtSandboxConfiguration(
        settings_dir=str(cfg_dir),
        default_path=str(_default_config_path()),
    )


def get_sandbox_status() -> Dict[str, object]:
    """Return global sandbox *availability* status.

    There is no process-wide sandbox configuration; this is intended
    for UIs to show whether sandboxing can be effective when enabled in
    a thread.
    """

    warning: Optional[str] = None
    if sandbox_enabled() and not sandbox_available():
        warning = (
            "Sandboxing is enabled by default but the 'srt' CLI was not found. "
            "Tool commands will run *without* a sandbox. Install it "
            "with: npm install -g @anthropic-ai/sandbox-runtime."
        )

    cfg = get_srt_sandbox_configuration()
    return {
        "available": sandbox_available(),
        "srt_bin": _SRT_BIN,
        "settings_dir": cfg.settings_dir,
        "default_config_path": cfg.default_path,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Internal helpers (effective settings file)
# ---------------------------------------------------------------------------


def _augment_with_protections(cfg: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of *cfg* with mandatory protections applied."""

    import copy

    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    fs = out.setdefault("filesystem", {})
    if not isinstance(fs, dict):
        fs = {}
        out["filesystem"] = fs

    deny = fs.get("denyWrite")
    if not isinstance(deny, list):
        deny = []

    # Always protect our settings directory and the default.json file.
    srt_dir = _ensure_srt_dir()
    protected = [str(srt_dir), str((srt_dir / "default.json").resolve())]
    for p in protected:
        if p not in deny:
            deny.append(p)
    fs["denyWrite"] = deny
    return out


def _effective_config_path_from_settings(settings: Dict[str, object]) -> Path:
    """Write an augmented settings file and return its path."""

    cfg_dir = _ensure_srt_dir()
    eff = _augment_with_protections(settings if isinstance(settings, dict) else {})

    # Content-addressed filename to avoid races between concurrent
    # invocations using different settings.
    try:
        canon = json.dumps(eff, sort_keys=True, separators=(",", ":")).encode("utf-8")
        h = hashlib.sha256(canon).hexdigest()[:16]
    except Exception:
        h = os.urandom(8).hex()

    eff_path = cfg_dir / f"_effective__{h}.json"
    try:
        eff_path.write_text(json.dumps(eff, indent=2), encoding="utf-8")
    except Exception:
        pass
    return eff_path

