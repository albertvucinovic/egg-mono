from __future__ import annotations

"""Sandbox helpers for tool execution.

This module centralises integration with the
``@anthropic-ai/sandbox-runtime`` CLI (``srt``).

Goals
-----

* Provide a **single place** where eggthreads decides whether tool
  executions (bash, python, etc.) should be wrapped in the sandbox.
* Keep configuration **per working directory** (the directory from
  which the process is started), under ``.egg/srt/``.
* Offer a small public API so callers can control the sandbox
  configuration and surface status in UIs.

Key concepts
------------

* The sandbox is considered **enabled by default** for the current
  process.  Applications (or UIs such as Egg's ``/toggleSandboxing``
  command) can enable or disable it at runtime via
  :func:`set_sandbox_globally_enabled`.

* The sandbox is considered **available** if an ``srt`` binary can be
  resolved.  The binary path can be overridden via ``EGG_SRT_BIN``.

* Effective behaviour:

  - If ``enabled`` *and* ``available`` → tool commands are wrapped as
    ``srt --settings <effective-config> <original argv...>``.
  - Otherwise the original argv is returned unchanged and callers run
    tools directly (unsandboxed).

* Configuration files live under ``.egg/srt/`` in the current working
  directory.  We create a default configuration file
  ``.egg/srt/default.json`` which is intentionally conservative:

    - On the filesystem, only the current directory is allowed for
      reading and writing.

* A *configuration name* selects which base config file to use from
  ``.egg/srt/<name>``.  For example, the UI command::

      /setSrtSandboxConfiguration readall.json

  corresponds to the file ``.egg/srt/readall.json`` in the current
  directory.  The active configuration name is process-local; callers
  may change it at runtime via :func:`set_srt_sandbox_configuration`.

* Regardless of which configuration is selected, **all files under
  ``.egg/srt/`` are always off limits for writing inside the sandbox**.
  We enforce this by augmenting every effective config with
  ``filesystem.denyWrite`` entries for the ``.egg/srt`` directory.

Public API
----------

* :func:`wrap_argv_for_sandbox(argv)` – prepend ``srt --settings ...``
  when sandboxing is active.  Used by tool implementations.
* :func:`set_srt_sandbox_configuration(name)` – select a base config
  file (relative name inside ``.egg/srt/``).
* :func:`get_srt_sandbox_configuration()` – inspect current
  configuration selection.
* :func:`get_srt_sandbox_status()` – return a dict describing
  enable/availability/effective status for UIs.

The implementation is deliberately self‑contained and avoids importing
other eggthreads modules to prevent circular imports.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import shutil


_CWD = Path(os.getcwd()).resolve()
_SRT_DIR = (_CWD / ".egg" / "srt").resolve()


def _ensure_srt_dir() -> Path:
    """Ensure ``.egg/srt`` exists and return its path."""

    try:
        _SRT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Best-effort; callers may still try to write configs and fail.
        pass
    return _SRT_DIR


def _default_config_dict() -> Dict[str, object]:
    """Return the in‑memory default sandbox configuration.

    The default is intentionally simple: it restricts filesystem access
    to the current working directory but leaves network access
    unrestricted ("allow all domains") so that existing tool usage
    with outbound HTTP continues to work without additional
    configuration.  Users can provide stricter configs under
    ``.egg/srt/`` and select them via :func:`set_srt_sandbox_configuration`.
    """

    return {
        # Allow outbound network access to a reasonable set of common
        # developer domains by default. Users who want tighter
        # restrictions can supply their own config with a more
        # constrained allowedDomains list.
        "network": {
            "allowedDomains": [
                "github.com",
                "*.github.com",
                "api.github.com",
                "npmjs.org",
                "*.npmjs.org",
                "pypi.org",
                "*.pypi.org",
            ],
            # Required by the sandbox-runtime schema; leave empty by
            # default so that only allowedDomains constraints apply.
            "deniedDomains": [],
        },
        "filesystem": {
            # Only allow reading/writing within the current directory by
            # default.  Paths are resolved relative to the sandboxed
            # process' CWD; we deliberately avoid absolute host paths
            # here so the config remains portable.
            "allowRead": ["."],
            "allowWrite": ["."],
            # denyRead/denyWrite will be extended at runtime to include
            # our own config directory.
            "denyRead": [],
            "denyWrite": [],
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
_SANDBOX_ENABLED: bool = False

_SRT_BIN = (os.environ.get("EGG_SRT_BIN") or "srt").strip() or "srt"
_SRT_AVAILABLE = shutil.which(_SRT_BIN) is not None


_CURRENT_CONFIG_NAME: str = "default.json"  # relative name inside .egg/srt


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


def _augment_with_protections(cfg: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of *cfg* with mandatory protections applied.

    In particular we ensure that the sandbox can never write into the
    ``.egg/srt`` directory which holds configuration files for this
    process, even if the user‑supplied config omitted such rules.
    """

    import copy

    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    fs = out.setdefault("filesystem", {})
    if not isinstance(fs, dict):
        fs = {}
        out["filesystem"] = fs

    deny = fs.get("denyWrite")
    if not isinstance(deny, list):
        deny = []

    protected_paths = {str(_ensure_srt_dir())}
    for p in protected_paths:
        if p not in deny:
            deny.append(p)
    fs["denyWrite"] = deny
    return out


def _effective_config_path() -> Path:
    """Return the path to the *effective* config used for sandbox runs.

    This function reads the currently selected source config
    (``.egg/srt/<name>`` or ``default.json``), augments it with the
    mandatory protections and writes the result to
    ``.egg/srt/_effective.json``.  The returned path is passed to
    ``srt --settings``.
    """

    cfg_dir = _ensure_srt_dir()
    src_path = _config_source_path(_CURRENT_CONFIG_NAME)
    cfg = _load_config(src_path)
    eff = _augment_with_protections(cfg)
    eff_path = cfg_dir / "_effective.json"
    try:
        eff_path.write_text(json.dumps(eff, indent=2), encoding="utf-8")
    except Exception:
        # Best-effort: if writing fails the sandbox wrapper will still
        # try to reference this path, and srt will fail fast.
        pass
    return eff_path


def sandbox_enabled() -> bool:
    """Return whether sandboxing is logically enabled.

    This does **not** guarantee that ``srt`` is available; see
    :func:`get_srt_sandbox_status`.
    """

    return _SANDBOX_ENABLED


def sandbox_available() -> bool:
    """Return whether an ``srt`` binary is available."""

    return _SRT_AVAILABLE


def set_sandbox_globally_enabled(enabled: bool) -> None:
    """Enable or disable sandboxing for this process.

    When ``enabled`` is False, :func:`wrap_argv_for_sandbox` will
    never inject ``srt`` and tools run unsandboxed.  When True,
    sandboxing becomes active as long as the ``srt`` binary is
    available.  This is the primary programmatic toggle used by UIs
    such as Egg's ``/toggleSandboxing`` command.
    """

    global _SANDBOX_ENABLED
    _SANDBOX_ENABLED = bool(enabled)


def is_sandbox_effective() -> bool:
    """Return True if tool commands will actually be sandboxed."""

    return sandbox_enabled() and sandbox_available()


def wrap_argv_for_sandbox(argv: List[str]) -> List[str]:                    
    """Return ``argv`` wrapped with ``srt --settings`` when active.         
                                                                            
    Args:                                                                   
        argv: The original command argv, e.g. ``["/bin/bash", "-lc", script]
                                                                            
    Returns:                                                                
        A new argv list. If sandboxing is disabled or unavailable, this is  
        identical to the input.                                             
                                                                            
        When sandboxing is enabled:                                         
                                                                            
        * For generic commands, we return::                                 
                                                                            
              ["srt", "--settings", "<path>", *argv]                        
                                                                            
        * For the specific ``/bin/bash -lc <script>`` pattern used by the   
          bash tool, we adapt to how `srt` expects its command argument and 
          delegate to :func:`wrap_bash_argv_for_sandbox`.                   
    """                                                                     
    if not is_sandbox_effective():                                          
        return argv                                                         
                                                                            
    eff_path = _effective_config_path()                                     
                                                                            
    # Special handling for the bash tool: `/bin/bash -lc <script>`.         
    # We want to emulate the working shell form:                            
    #                                                                       
    #   srt "/bin/bash -lc '<script>'"                                      
    #                                                                       
    # rather than the broken:                                               
    #                                                                       
    #   srt "/bin/bash -lc ls -la"                                          
    #                                                                       
    if (                                                                    
        len(argv) >= 3                                                      
        and argv[0] == "/bin/bash"                                          
        and argv[1] in ("-lc", "-c")                                        
    ):                                                                      
        return wrap_bash_argv_for_sandbox(argv, eff_path)                   
                                                                            
    # Generic case: just prepend the sandbox launcher.                      
    return [_SRT_BIN, "--settings", str(eff_path), *argv]                   


def wrap_bash_argv_for_sandbox(argv: List[str], eff_path) -> List[str]:              
    """Wrap a `/bin/bash -lc <script>` argv for execution under `srt`.               
                                                                                     
    This builds an argv equivalent to the working shell invocation:                  
                                                                                     
        srt "/bin/bash -lc '<script>'"                                               
                                                                                     
    so that `srt` receives the entire command as a single argument and               
    preserves the intended `bash -lc 'script'` semantics.                            
    """                                                                              
    # Defensive: if we somehow don't have a script, fall back to generic wrap.       
    if len(argv) <= 2:                                                               
        return [_SRT_BIN, "--settings", str(eff_path), *argv]                        
                                                                                     
    # Everything after the -c/-lc flag is the script. For the bash tool this         
    # will typically be a single element, but we join in case there are more.        
    script = " ".join(argv[2:])                                                      
                                                                                     
    # Shell-style single quoting without using `shlex`:                              
    # - Wrap the whole string in single quotes.                                      
    # - Replace each internal single quote ' with '\'' (close, escaped quote, reopen)
    if script:                                                                       
        script_quoted = "'" + script.replace("'", "'\\''") + "'"                     
    else:                                                                            
        # Empty script: still represent it explicitly for the shell.                 
        script_quoted = "''"                                                         
                                                                                     
    # Build a single command string for srt, e.g.:                                   
    #   "/bin/bash -lc 'ls -la'"                                                     
    cmd_str = f"{argv[0]} {argv[1]} {script_quoted}"                                 
                                                                                     
    return [_SRT_BIN, "--settings", str(eff_path), cmd_str]                          



def set_srt_sandbox_configuration(name: str) -> None:
    """Select a sandbox configuration file for this process.

    ``name`` is interpreted as a file name inside ``.egg/srt`` relative
    to the current working directory.  For example, ``"readall.json"``
    refers to ``.egg/srt/readall.json``.

    If the file does not exist, :class:`ValueError` is raised and the
    active configuration is left unchanged.
    """

    global _CURRENT_CONFIG_NAME

    norm = _normalize_name(name)
    cfg_dir = _ensure_srt_dir()
    path = cfg_dir / norm
    if not path.exists():
        raise ValueError(f"srt configuration file not found: {path}")

    _CURRENT_CONFIG_NAME = norm


@dataclass
class SrtSandboxConfiguration:
    """Simple struct describing the current configuration selection."""

    name: str
    settings_dir: str
    source_path: str


def get_srt_sandbox_configuration() -> SrtSandboxConfiguration:
    """Return the currently selected sandbox configuration metadata."""

    cfg_dir = _ensure_srt_dir()
    src = _config_source_path(_CURRENT_CONFIG_NAME)
    return SrtSandboxConfiguration(
        name=_normalize_name(_CURRENT_CONFIG_NAME),
        settings_dir=str(cfg_dir),
        source_path=str(src),
    )


def get_srt_sandbox_status() -> Dict[str, object]:
    """Return a summary of sandbox status for UIs.

    The returned dict contains at least:

    - ``enabled`` (bool): sandboxing logically enabled.
    - ``available`` (bool): ``srt`` binary was found.
    - ``effective`` (bool): tools will actually be sandboxed.
    - ``mode`` (str): "on" or "off" representing the current
      process-wide sandbox toggle.
    - ``srt_bin`` (str): binary name/path used for ``srt``.
    - ``config_name`` (str): current configuration name.
    - ``config_path`` (str): path to the selected source config file.
    - ``settings_dir`` (str): directory holding configuration files.
    - ``warning`` (Optional[str]): human-readable warning message when
      sandboxing is enabled but not available.
    """

    cfg = get_srt_sandbox_configuration()
    effective = is_sandbox_effective()
    warning: Optional[str] = None
    if sandbox_enabled() and not sandbox_available():
        warning = (
            "Sandboxing is enabled but the 'srt' CLI was not found. "
            "Tool commands will run *without* a sandbox. Install it "
            "with: npm install -g @anthropic-ai/sandbox-runtime."
        )

    return {
        "enabled": sandbox_enabled(),
        "available": sandbox_available(),
        "effective": effective,
        "mode": "on" if sandbox_enabled() else "off",
        "srt_bin": _SRT_BIN,
        "config_name": cfg.name,
        "config_path": cfg.source_path,
        "settings_dir": cfg.settings_dir,
        "warning": warning,
    }

