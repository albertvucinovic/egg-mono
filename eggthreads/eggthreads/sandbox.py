from __future__ import annotations

"""Sandbox helpers for tool execution.

This module centralises integration with the
``@anthropic-ai/sandbox-runtime`` CLI (``srt``).

Goals
-----

* Provide a **single place** where eggthreads decides whether tool
  executions (bash, python, etc.) should be wrapped in the sandbox.
* Keep a **default** configuration per working directory (the directory
  from which the process is started), under ``.egg/sandbox/default.json``.

* Support **per-thread** sandbox configuration via DB events:

  - If neither the thread nor any ancestor has a config event, the
    default settings file ``.egg/sandbox/default.json`` is used.

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

* Configuration files live under ``.egg/sandbox/`` in the current working
  directory.  We create a default configuration file
  ``.egg/sandbox/default.json`` which is intentionally conservative:

    - On the filesystem, only the current directory is allowed for
      reading and writing.

* Optional *named* configuration files may exist under ``.egg/sandbox/``
  (e.g. ``.egg/sandbox/readall.json``). UIs may load such a file and store
  the full JSON into a ``sandbox.config`` event.

* Regardless of which configuration is used, **all files under
  ``.egg/sandbox/`` are always off limits for writing inside the sandbox**.
  In particular, ``.egg/sandbox/default.json`` must never be writable from
  within the sandbox. We enforce this by augmenting every effective
  config with mandatory ``filesystem.denyWrite`` entries.

Public API
----------

* :func:`wrap_argv_for_sandbox_with_settings(argv, enabled, settings)` –
  prepend ``srt --settings ...`` when sandboxing is effective.
* :func:`get_thread_sandbox_config(db, thread_id)` – resolve the
  effective per-thread configuration (including ancestor inheritance).
* User sandbox control: a ``user_control_enabled`` field in the configuration`` determines whether UI commands can modify sandbox settings for a thread.
* :func:`set_thread_sandbox_config(db, thread_id, ...)` – persist a
  per-thread configuration as an event containing the full JSON.
* :func:`get_thread_sandbox_status(db, thread_id)` – status dict for UIs.

The implementation is deliberately self‑contained and avoids importing
other eggthreads modules to prevent circular imports.
"""

from dataclasses import dataclass, field
import json
import os
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

import shutil


if TYPE_CHECKING:  # pragma: no cover
    from .db import ThreadsDB



# ---------------------------------------------------------------------------
# Common abstractions for mandatory protections and default configurations
# ---------------------------------------------------------------------------

def get_mandatory_protected_paths() -> List[str]:
    """Return list of paths that must be protected in all sandbox configurations.
    
    These paths should be made read-only or otherwise protected from writes
    regardless of provider or configuration.
    """
    sandbox_dir = _ensure_sandbox_dir()
    egg_dir = sandbox_dir.parent
    return [
        str(sandbox_dir),
        str((sandbox_dir / "default.json").resolve()),
        str(egg_dir),
    ]


_default_docker_image_cache = None

def _default_docker_image() -> str:
    """Return the default Docker image for sandboxing.
    
    If the locally built egg-sandbox image exists, use that.
    Otherwise fall back to python:3.12-slim.
    """
    global _default_docker_image_cache
    if _default_docker_image_cache is not None:
        return _default_docker_image_cache
    
    # Check if docker CLI is available
    import subprocess
    try:
        # First, check if docker is reachable
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
    except Exception:
        # Docker not available, fall back to public image
        _default_docker_image_cache = "python:3.12-slim"
        return _default_docker_image_cache
    
    # Docker is available, check for local egg-sandbox image
    try:
        subprocess.run(
            ["docker", "image", "inspect", "egg-sandbox"],
            capture_output=True,
            check=True,
            timeout=5,
        )
        _default_docker_image_cache = "egg-sandbox"
    except Exception:
        _default_docker_image_cache = "python:3.12-slim"
    return _default_docker_image_cache

def get_provider_default_config(provider_name: str) -> Dict[str, object]:
    """Return default configuration suitable for a specific provider.
    
    Each provider has different configuration needs. This function returns
    sensible defaults for each provider type.
    """
    # Base defaults are SRT-style config
    base_defaults = _default_config_dict()
    
    if provider_name == "docker":
        return {
            "provider": "docker",
            "image": _default_docker_image(),
            "network": "none",
            "workspace": "/workspace",
            "extra_mounts": [],
            "extra_args": ["--cap-drop", "ALL"],
        }
    elif provider_name == "bwrap":
        return {
            "provider": "bwrap",
            # Minimal settings - bwrap primarily uses working directory binding
        }
    elif provider_name == "srt":
        return base_defaults
    else:
        # Unknown provider, return base defaults
        return base_defaults


def apply_mandatory_protections(provider_name: str, settings: Dict[str, Any], 
                               working_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Apply mandatory protections to settings for any provider.
    
    This ensures consistent protection of critical paths (.egg directory, etc.)
    across all providers, using each provider's native mechanism.
    
    Returns: Updated settings with protections applied
    """
    import copy
    
    # Make a copy to avoid modifying original
    result = copy.deepcopy(settings) if isinstance(settings, dict) else {}
    
    # Get mandatory protected paths
    protected_paths = get_mandatory_protected_paths()
    
    if provider_name == "srt":
        # For SRT, we need to update the filesystem.denyWrite list
        fs = result.setdefault("filesystem", {})
        if not isinstance(fs, dict):
            fs = {}
            result["filesystem"] = fs
            
        deny_write = fs.get("denyWrite")
        if not isinstance(deny_write, list):
            deny_write = []
            
        for path in protected_paths:
            if path not in deny_write:
                deny_write.append(path)
                
        fs["denyWrite"] = deny_write
        
        # Also add to denyRead
        deny_read = fs.get("denyRead")
        if not isinstance(deny_read, list):
            deny_read = []
            
        egg_dir = _ensure_sandbox_dir().parent
        if str(egg_dir) not in deny_read:
            deny_read.append(str(egg_dir))
            
        fs["denyRead"] = deny_read
        
    # For docker and bwrap, protections are applied in wrap_argv methods
    # using the protected_paths list. They don't need settings modification.
    
    return result

def normalize_provider_settings(provider_name: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize settings for a specific provider, filling in defaults.
    
    Takes user-provided settings and ensures all required fields are present
    with sensible defaults.
    """
    if not isinstance(settings, dict):
        settings = {}
        
    # Get provider defaults
    defaults = get_provider_default_config(provider_name)
    
    # Merge user settings over defaults
    result = defaults.copy()
    result.update(settings)
    
    # Ensure provider field is correct
    result["provider"] = provider_name
    
    return result

@dataclass
class ThreadSandboxConfig:
    """Effective sandbox selection for a thread."""

    enabled: bool
    provider: str
    settings: Dict[str, object]
    source: str
    user_control_enabled: bool = field(default=True)


# ---------------------------------------------------------------------------
# Sandbox providers registry
# ---------------------------------------------------------------------------

from typing import Protocol, runtime_checkable, Dict, List, Optional, Any
import subprocess
import shutil


@runtime_checkable
class SandboxProvider(Protocol):
    """Protocol for sandbox providers."""
    name: str

    def is_available(self) -> bool:
        """Return True if this provider can be used on the current system."""
        ...

    def wrap_argv(self, argv: List[str], settings: Dict[str, Any], working_dir: Optional[Path] = None) -> List[str]:
        if not self.is_available():
            return argv
        # Basic bwrap sandbox: read-only root, bind working directory, unshare network
        # This is a minimal example; real implementation should respect settings.
        from pathlib import Path
        wd = Path(working_dir).resolve() if working_dir else Path.cwd().resolve()
        # Build bwrap command
        cmd = ["bwrap", "--ro-bind", "/", "/",
               "--bind", str(wd), str(wd),
               "--dev", "/dev",
               "--proc", "/proc",
               "--unshare-net",
               "--chdir", str(wd)]
        # Protect .egg directory if it's within the bound directory
        egg_dir = Path.cwd() / ".egg"
        try:
            egg_rel = egg_dir.relative_to(wd)
            # egg_dir is inside wd, need to add read-only bind
            cmd.extend(["--ro-bind", str(egg_dir), str(egg_dir)])
        except ValueError:
            # egg_dir is not inside wd, already protected by root ro-bind
            pass
        # Add the original command
        cmd.extend(argv)
        return cmd
        srt_bin = os.environ.get("EGG_SRT_BIN", "srt").strip() or "srt"
        available = shutil.which(srt_bin) is not None
        return {
            "available": available,
            "binary": srt_bin,
        }

class DockerProvider:
    """Sandbox provider using Docker containers."""
    name = "docker"

    def is_available(self) -> bool:
        # Check if docker CLI is available and daemon reachable.
        try:
            import subprocess
            subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
            return True
        except Exception:
            return False

    def wrap_argv(self, argv: List[str], settings: Dict[str, Any], working_dir: Optional[Path] = None) -> List[str]:
        if not self.is_available():
            # Normalize settings with provider-specific defaults
            settings = normalize_provider_settings("docker", settings)

            return argv
        # Default settings
        image = settings.get("image", _default_docker_image())
        network = settings.get("network", "none")

        # Ensure network is a string (srt-style settings use dict)
        if not isinstance(network, str):
            network = "none"
        workspace = settings.get("workspace", "/workspace")
        extra_mounts = settings.get("extra_mounts", [])
        extra_args = settings.get("extra_args", [])
        # Build docker run command
        cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}"]
        # Network
        if network:
            cmd.extend(["--network", network])
        # Mount working directory as workspace
        from pathlib import Path
        wd = Path(working_dir).resolve() if working_dir else Path.cwd().resolve()
        cmd.extend(["-v", f"{wd}:{workspace}"])
        # Protect mandatory paths (e.g., .egg directory)
        protected_paths = get_mandatory_protected_paths()
        for protected in protected_paths:
            try:
                prot_path = Path(protected).resolve()
                if prot_path.exists():
                    rel_path = prot_path.relative_to(wd)
                    container_path = str(Path(workspace) / rel_path)
                    cmd.extend(["-v", f"{prot_path}:{container_path}:ro"])
            except (ValueError, Exception):
                # Path not inside working directory or other error
                pass
        for mount in extra_mounts:
            if isinstance(mount, dict) and mount.get("src") and mount.get("dst"):
                cmd.extend(["-v", f"{mount['src']}:{mount['dst']}"])
        # Extra arguments (user-provided)
        for arg in extra_args:
            if isinstance(arg, str):
                cmd.append(arg)
        # Set working directory inside container
        cmd.extend(["-w", workspace])
        # Image
        cmd.append(image)
        # The command to run inside container (argv)
        cmd.extend(argv)
        return cmd

    def get_status(self) -> Dict[str, Any]:
        available = self.is_available()
        return {
            "available": available,
            "provider": "docker",
        }

class BwrapProvider:
    """Sandbox provider using bubblewrap (bwrap)."""
    name = "bwrap"

    def is_available(self) -> bool:
        # Check if bwrap binary exists
        import shutil
        return shutil.which("bwrap") is not None

    def wrap_argv(self, argv: List[str], settings: Dict[str, Any], working_dir: Optional[Path] = None) -> List[str]:
        if not self.is_available():
            # Normalize settings with provider-specific defaults
            settings = normalize_provider_settings("bwrap", settings)
        
            return argv
        # Basic bwrap sandbox: read-only root, bind working directory, unshare network
        # This is a minimal example; real implementation should respect settings.
        from pathlib import Path
        wd = Path(working_dir).resolve() if working_dir else Path.cwd().resolve()
        # Build bwrap command
        cmd = ["bwrap", "--ro-bind", "/", "/",
               "--bind", str(wd), str(wd),
               "--dev", "/dev",
               "--proc", "/proc",
               "--unshare-net",
               "--chdir", str(wd)]
        # Protect mandatory paths (e.g., .egg directory)
        protected_paths = get_mandatory_protected_paths()
        for protected in protected_paths:
            try:
                prot_path = Path(protected).resolve()
                if prot_path.exists():
                    # Only protect if path is inside working directory
                    _ = prot_path.relative_to(wd)
                    cmd.extend(["--ro-bind", str(prot_path), str(prot_path)])
            except (ValueError, Exception):
                # Path not inside working directory or other error
                pass
        cmd.extend(argv)
        return cmd
    def get_status(self) -> Dict[str, Any]:
        available = self.is_available()
        return {
            "available": available,
            "provider": "bwrap",
        }
# Registry of known providers
class SrtProvider:
    """Sandbox provider using Anthropic's sandbox-runtime (srt)."""
    name = "srt"

    def is_available(self) -> bool:
        srt_bin = os.environ.get("EGG_SRT_BIN", "srt").strip() or "srt"
        return shutil.which(srt_bin) is not None

    def wrap_argv(self, argv: List[str], settings: Dict[str, Any], working_dir: Optional[Path] = None) -> List[str]:
        # If not available, return original argv (caller will fall back).
        if not self.is_available():
            return argv
        
        # Apply mandatory protections and normalize settings
        cfg = apply_mandatory_protections("srt", settings, working_dir)
        
        # Add working_dir to allowWrite if it's a subdirectory of CWD
        if working_dir:
            try:
                wd = Path(working_dir).resolve()
                cwd = Path.cwd().resolve()
                rel_wd = wd.relative_to(cwd)
                fs = cfg.setdefault("filesystem", {})
                if not isinstance(fs, dict):
                    fs = {}
                    cfg["filesystem"] = fs
                aw = fs.get("allowWrite")
                if not isinstance(aw, list):
                    aw = ["."]
                if str(rel_wd) not in aw:
                    aw.append(str(rel_wd))
                fs["allowWrite"] = aw
            except ValueError:
                # Not a subdirectory, keep settings as is (srt may deny it)
                pass
        
        # Use the existing helper to augment with mandatory protections and write file
        eff_path = _effective_config_path_from_settings(cfg)
        # Build command string
        try:
            import shlex
            cmd_str = shlex.join(list(argv))
        except Exception:
            cmd_str = " ".join(str(x) for x in argv)
        srt_bin = os.environ.get("EGG_SRT_BIN", "srt").strip() or "srt"
        return [srt_bin, "--settings", str(eff_path), cmd_str]
    def get_status(self) -> Dict[str, Any]:
        srt_bin = os.environ.get("EGG_SRT_BIN", "srt").strip() or "srt"
        available = shutil.which(srt_bin) is not None
        return {
            "available": available,
            "binary": srt_bin,
        }

_PROVIDERS: Dict[str, SandboxProvider] = {
    "srt": SrtProvider(),
    "docker": DockerProvider(),
    "bwrap": BwrapProvider(),}


def _get_provider(name: str) -> Optional[SandboxProvider]:
    return _PROVIDERS.get(name)


def provider_available(name: str) -> bool:
    provider = _get_provider(name)
    return provider.is_available() if provider else False


def get_provider_names() -> List[str]:
    return list(_PROVIDERS.keys())


# ---------------------------------------------------------------------------
def _sandbox_dir() -> Path:
    """Return the per-working-directory settings dir (``.egg/sandbox``).

    We intentionally compute this dynamically from :func:`Path.cwd`
    instead of capturing the CWD at import time. This keeps the module
    robust in test suites (which often chdir) and in interactive usage
    where callers may change directories.
    """

    return (Path.cwd() / ".egg" / "sandbox").resolve()


def _ensure_sandbox_dir() -> Path:
    """Ensure ``.egg/sandbox`` exists and return its path."""

    sandbox_dir = _sandbox_dir()
    try:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Best-effort; callers may still try to write configs and fail.
        pass
    return sandbox_dir


def _default_config_dict() -> Dict[str, object]:
    """Return the in‑memory default sandbox configuration.

    The default is intentionally simple: it restricts filesystem access
    to the current working directory but leaves network access
    unrestricted ("allow all domains") so that existing tool usage
    with outbound HTTP continues to work without additional
    configuration.  Users can provide stricter configs under
    ``.egg/sandbox/`` and select them via :func:`set_srt_sandbox_configuration`.
    """

    # NOTE: sandbox-runtime currently supports a deny-only model for
    # reads (``filesystem.denyRead``). That means we cannot express
    # "allow reads only under ." in a portable way. We *can* express
    # "allow writes only under ." (``filesystem.allowWrite``).

    return {
        # The default sandbox provider is docker (container-based).
        "provider": "docker",
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
            # Denies will be extended at runtime to always protect the
            # .egg directory (including settings).
            "denyWrite": [".egg"],
        },
    }
def _default_config_path() -> Path:
    """Return the path to the default config, creating it if needed."""

    cfg_dir = _ensure_sandbox_dir()
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
    # Prevent directory traversal outside .egg/sandbox – treat name as a
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

    cfg_dir = _ensure_sandbox_dir()
    if not name:
        return _default_config_path()
    norm = _normalize_name(name)
    path = cfg_dir / norm
    if path.exists():
        return path
    return _default_config_path()


def _augment_with_protections(cfg: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of *cfg* with mandatory protections applied."""

    import copy

    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    fs = out.setdefault("filesystem", {})
    if not isinstance(fs, dict):
        fs = {}
        out["filesystem"] = fs

    # Add .egg to denyRead if present
    deny_read = fs.get("denyRead")
    if not isinstance(deny_read, list):
        deny_read = []
    egg_dir = _ensure_sandbox_dir().parent
    if str(egg_dir) not in deny_read:
        deny_read.append(str(egg_dir))
    fs["denyRead"] = deny_read

    deny = fs.get("denyWrite")
    if not isinstance(deny, list):
        deny = []

    # Always protect our settings directory and the default.json file.
    protected = get_mandatory_protected_paths()
    for p in protected:
        if p not in deny:
            deny.append(p)
    fs["denyWrite"] = deny
    return out
def _effective_config_path_from_settings(settings: Dict[str, object]) -> Path:
    """Write an augmented settings file and return its path."""

    cfg_dir = _ensure_sandbox_dir()
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
    """Return whether the default sandbox provider (docker) is available."""
    try:
        return _PROVIDERS["docker"].is_available()
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
      * validates that the named config exists under .egg/sandbox.

    Callers that want a thread to actually use the config must store it
    into the thread via :func:`set_thread_sandbox_config`.
    """

    set_sandbox_globally_enabled(bool(enabled))
    if isinstance(config_name, str) and config_name.strip():
        norm = _normalize_name(config_name)
        cfg_dir = _ensure_sandbox_dir()
        path = cfg_dir / norm
        if not path.exists():
            raise ValueError(f"sandbox configuration file not found: {path}")


def set_srt_sandbox_configuration(name: str) -> None:
    """Backward compatible alias."""

    set_sandbox_config(enabled=sandbox_enabled(), config_name=name)


def is_sandbox_effective() -> bool:
    """Return True if tool commands will actually be sandboxed."""
    return sandbox_enabled() and sandbox_available()


def wrap_argv_for_sandbox(argv: List[str]) -> List[str]:
    """Backward-compatible convenience wrapper.

    If called without thread context, we use the default settings
    (``.egg/sandbox/default.json``) and the default enabled policy.
    """
    return wrap_argv_for_sandbox_with_settings(
        argv,
        enabled=sandbox_enabled(),
        settings=_load_config(_default_config_path()),
        provider="docker",
    )
def wrap_argv_for_sandbox_with_settings(
    argv: List[str],
    *,
    enabled: bool,
    settings: Dict[str, object],
    working_dir: Optional[str | Path] = None,
    provider: Optional[str] = None,
) -> List[str]:
    """Wrap an argv for sandbox execution with explicit settings.

    The provider can be specified via the ``provider`` argument or via a
    "provider" key inside ``settings`` (default "docker").  If sandboxing is
    disabled or the requested provider is unavailable, the original argv
    is returned unchanged.
    """
    if not enabled:
        return argv

    # Determine provider name
    if provider is None:
        provider_name = str(settings.get("provider", "docker"))
    else:
        provider_name = provider

    provider_obj = _PROVIDERS.get(provider_name)
    # Normalize settings for this provider
    settings = normalize_provider_settings(provider_name, settings)
        # Apply mandatory protections
    settings = apply_mandatory_protections(provider_name, settings, working_dir)
    
    if provider_obj is None:
        # Unknown provider -> no sandbox
        return argv
    if not provider_obj.is_available():
        return argv

    # Delegate to provider
    return provider_obj.wrap_argv(argv, settings, working_dir)
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
    return wrap_argv_for_sandbox_with_settings(argv, enabled=eff_enabled, settings=settings, provider="docker")
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
    3) ``.egg/sandbox/default.json`` (created if missing).

    The returned config contains the full settings dict. Mandatory
    protections (e.g. denying writes to ``.egg/sandbox``) are applied at
    execution time when we write the *effective* settings file.
    """

    enabled = sandbox_enabled()
    provider = "docker"
    user_control_enabled = True
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

        # Provider (default "srt" for backward compatibility)
        if "provider" in payload:
            prov = payload.get("provider")
            if isinstance(prov, str) and prov.strip():
                provider = prov.strip()
                # User control flag
                if "user_control_enabled" in payload:
                    try:
                        user_control_enabled = bool(payload.get("user_control_enabled"))
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

    return ThreadSandboxConfig(
        enabled=bool(enabled),
        provider=provider,
        settings=dict(settings),
        source=str(source),
        user_control_enabled=user_control_enabled,
    )
def set_thread_sandbox_config(
    db: "ThreadsDB",
    thread_id: str,
    *,
    enabled: bool,
    config_name: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    provider: Optional[str] = None, user_control_enabled: Optional[bool] = None,
    reason: str = "user",
) -> None:
    """Persist sandbox configuration for a thread.

    This appends a ``sandbox.config`` event so that the effective
    sandbox choice is reproducible across processes.
    """

    import os as _os
    # Determine user_control_enabled if not provided
    if user_control_enabled is None:
        # First check if settings dict contains user_control_enabled
        if isinstance(settings, dict) and "user_control_enabled" in settings:
            try:
                user_control_enabled = bool(settings.get("user_control_enabled"))
            except Exception:
                pass
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
            cfg_dir = _ensure_sandbox_dir()
            path = cfg_dir / norm
            if not path.exists():
                raise ValueError(f"sandbox configuration file not found: {path}")
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

    # If user_control_enabled still not determined, check payload["settings"]
    if user_control_enabled is None:
        payload_settings = payload.get("settings")
        if isinstance(payload_settings, dict) and "user_control_enabled" in payload_settings:
            try:
                user_control_enabled = bool(payload_settings.get("user_control_enabled"))
            except Exception:
                pass
    # Final fallback to current config
    if user_control_enabled is None:
        cfg = get_thread_sandbox_config(db, thread_id)
        user_control_enabled = cfg.user_control_enabled
    # Provider (default "docker")
    if provider is not None:
        payload["provider"] = provider
    else:
        # Infer from settings if present, otherwise default to "docker"
        # Check both the settings parameter and payload["settings"]
        prov = None
        if isinstance(settings, dict):
            prov = settings.get("provider")
        if not isinstance(prov, str) or not prov.strip():
            # Check payload settings
            payload_settings = payload.get("settings")
            if isinstance(payload_settings, dict):
                prov = payload_settings.get("provider")
        if isinstance(prov, str) and prov.strip():
            payload["provider"] = prov.strip()
        else:
            payload["provider"] = "docker"
    if user_control_enabled is not None:
        payload["user_control_enabled"] = user_control_enabled
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
    provider_available = _PROVIDERS.get(cfg.provider, SrtProvider()).is_available()
    effective = bool(cfg.enabled) and provider_available
    warning: Optional[str] = None
    if bool(cfg.enabled) and not provider_available:
        warning = (
            f"Sandboxing is enabled but provider '{cfg.provider}' is not available. "
            "Tool commands will run *without* a sandbox."
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
        "available": provider_available,
        "effective": effective,
        "mode": "on" if bool(cfg.enabled) else "off",
        "provider": cfg.provider,
        "config_source": cfg.source,
        "config_path": str(src),
        "settings_dir": str(_ensure_sandbox_dir()),
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

def enable_user_sandbox_control(db: "ThreadsDB", thread_id: str, reason: Optional[str] = None) -> None:
    """Allow user commands /toggleSandboxing and /setSandboxConfiguration for this thread.

    This is a thread-wide flag that can only be set programmatically via this API.
    When disabled, the UI commands that modify sandbox configuration are blocked.
    """
    # Get current config to preserve settings
    cfg = get_thread_sandbox_config(db, thread_id)
    set_thread_sandbox_config(
        db, thread_id,
        enabled=cfg.enabled,
        provider=cfg.provider,
        settings=cfg.settings,
        user_control_enabled=True,
        reason=reason or "enable_user_sandbox_control"
    )
def disable_user_sandbox_control(db: "ThreadsDB", thread_id: str, reason: Optional[str] = None) -> None:
    """Disallow user commands /toggleSandboxing and /setSandboxConfiguration for this thread.

    This is a thread-wide flag that can only be set programmatically via this API.
    When disabled, the UI commands that modify sandbox configuration are blocked.
    """
    # Get current config to preserve settings
    cfg = get_thread_sandbox_config(db, thread_id)
    set_thread_sandbox_config(
        db, thread_id,
        enabled=cfg.enabled,
        provider=cfg.provider,
        settings=cfg.settings,
        user_control_enabled=False,
        reason=reason or "disable_user_sandbox_control"
    )
def is_user_sandbox_control_enabled(db: "ThreadsDB", thread_id: str) -> bool:
    """Return True if user sandbox control commands are allowed for this thread.

    Defaults to True (allowed) when no sandbox.config event exists.
    """
    cfg = get_thread_sandbox_config(db, thread_id)
    return cfg.user_control_enabled
@dataclass
class SrtSandboxConfiguration:
    """Metadata about the per-working-directory sandbox settings."""

    settings_dir: str
    default_path: str


def get_srt_sandbox_configuration() -> SrtSandboxConfiguration:
    """Return metadata for the working-directory settings folder."""

    cfg_dir = _ensure_sandbox_dir()
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
            "Sandboxing is enabled by default but the 'docker' provider is not available. "
            "Tool commands will run *without* a sandbox. Install Docker or choose another provider."
        )

    cfg = get_srt_sandbox_configuration()
    # Provider availability
    providers = {}
    for name, prov in _PROVIDERS.items():
        providers[name] = prov.is_available()
    return {
        "available": sandbox_available(),
        "srt_bin": _SRT_BIN,
        "settings_dir": cfg.settings_dir,
        "default_config_path": cfg.default_path,
        "warning": warning,
        "providers": providers,
    }
