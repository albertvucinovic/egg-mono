from __future__ import annotations

"""Built-in sandbox administration commands."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext


SANDBOX_CONFIGURATION_HELP = """\
Sandbox Configuration and Control in Eggthreads

1. Default Configuration Handling and Storage

Default Configuration Creation:

 • The default sandbox configuration is defined in _default_config_dict() in sandbox.py
 • By default, it sets "provider": "docker" (previously "srt")
 • Other default settings include network restrictions and filesystem permissions

Storage Location:

 • Default configuration is stored in .egg/sandbox/default.json (legacy directory name)
 • This file is created automatically if it doesn't exist when _default_config_path() is called
 • The file location is relative to the current working directory

Provider-Specific Defaults:

 • Docker Provider: Defaults to image: "python:3.12-slim", network: "none", workspace: "/workspace"
 • SRT Provider: Uses SRT-specific settings like filesystem.allowWrite and network.allowedDomains
 • Bwrap Provider: Uses minimal settings, primarily working directory binding

Configuration Inheritance:

 • Threads inherit sandbox configuration from their nearest ancestor with a sandbox.config event
 • If no ancestor has a config, the default config from .egg/sandbox/default.json is used

2. Specifying Configuration for Each Provider

Provider Selection:

 • The provider field in settings determines which provider to use
 • Can be specified via: settings["provider"], provider parameter, or default config
 • Supported values: "docker", "srt", "bwrap"

Provider-Specific Settings:

Docker Provider:

 {
   "provider": "docker",
   "image": "python:3.12-slim",
   "network": "none",
   "workspace": "/workspace",
   "extra_mounts": [{"src": "/host/path", "dst": "/container/path"}],
   "extra_args": ["--cap-drop", "ALL"]
 }

SRT Provider:

 {
   "provider": "srt",
   "filesystem": {
     "allowWrite": ["."],
     "denyWrite": [".egg"]
   },
   "network": {
     "allowedDomains": ["example.com"]
   }
 }

Bwrap Provider:

 {
   "provider": "bwrap"
   // Minimal settings - primarily uses working directory
 }

Configuration Methods:

 1 Thread-specific config: set_thread_sandbox_config(db, thread_id, enabled=True, provider="docker", settings={...})
 2 Config files: Store JSON files in .egg/sandbox/ and reference by name
 3 Programmatic settings: Pass settings dict directly to API functions

3. Sandbox Control from egg.py TUI

Commands Available:

 1 /toggleSandboxing - Toggle sandboxing for current thread subtree
    • Toggles enabled flag while preserving current settings
    • Updates thread's sandbox.config event
    • Shows status in System panel
 2 /setSandboxConfiguration <file.json> - Apply config file to current thread
    • Loads JSON file from .egg/sandbox/ directory
    • Applies full settings to current thread and subtree
    • File should contain provider field and provider-specific settings

UI Indicators:

 • System Panel Title: Shows Sandboxing[ON] (green) or Sandboxing[OFF] (red)
 • Status based on: get_thread_sandbox_status() effectiveness check
 • Warning messages: Show if sandboxing enabled but provider unavailable

4. User Sandbox Control

User sandbox control determines whether the TUI commands /toggleSandboxing and
/setSandboxConfiguration are allowed for a thread. By default, user control is
enabled (True). It can be disabled programmatically via the eggthreads API.

Control Methods:

 1 Via configuration: Include "user_control_enabled": false in any sandbox
   configuration (JSON file or settings dict). When such a config is applied,
   the UI commands will be blocked for that thread and its descendants.

 2 Via API functions:
    • enable_user_sandbox_control(db, thread_id, reason) – allow UI commands
    • disable_user_sandbox_control(db, thread_id, reason) – block UI commands
    • is_user_sandbox_control_enabled(db, thread_id) – check current status

 3 Inheritance: The user_control_enabled flag is inherited through the same
   sandbox.config event mechanism as other sandbox settings. Child threads
   inherit the flag from their nearest ancestor.

When user control is disabled, attempts to use /toggleSandboxing or
/setSandboxConfiguration will show "User sandbox control is disabled for this
thread" in the System panel. This allows parent threads to lock sandbox
configuration for child threads (e.g., in puzzle solving or automated tasks).
""".strip()


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def _user_control_allowed(context: Any, db: Any, thread_id: str) -> bool:
    try:
        import eggthreads as _eggthreads

        if not _eggthreads.is_user_sandbox_control_enabled(db, thread_id):
            _log(context, "User sandbox control is disabled for this thread.")
            return False
    except ImportError:
        pass
    return True


def toggle_sandboxing_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _target(context, "toggleSandboxing")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    if not _user_control_allowed(context, db, thread_id):
        return CommandResult(clear_input=False)

    try:
        st = _eggthreads.get_thread_sandbox_status(db, thread_id)
        enabled_before = bool(st.get("enabled"))
        new_enabled = not enabled_before
        cfg = _eggthreads.get_thread_sandbox_config(db, thread_id)
        _eggthreads.set_thread_sandbox_config(
            db,
            thread_id,
            enabled=new_enabled,
            settings=cfg.settings,
            reason="/toggleSandboxing",
        )

        st2 = _eggthreads.get_thread_sandbox_status(db, thread_id)
        if bool(st2.get("effective")):
            _log(context, "Sandboxing ENABLED for this thread subtree.")
        else:
            warn = st2.get("warning")
            if isinstance(warn, str) and warn:
                _log(context, f"Sandboxing ENABLED but not effective: {warn}")
            else:
                _log(context, "Sandboxing ENABLED but not effective.")
    except Exception as e:
        _log(context, f"/toggleSandboxing error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def set_sandbox_configuration_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _target(context, "setSandboxConfiguration")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    if not _user_control_allowed(context, db, thread_id):
        return CommandResult(clear_input=False)

    name = (arg or "").strip()
    if not name:
        _log(context, "Sandbox configuration help (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Sandbox Configuration", SANDBOX_CONFIGURATION_HELP, border_style="blue")
        else:
            _log(context, SANDBOX_CONFIGURATION_HELP)
        return CommandResult(clear_input=True)

    try:
        _eggthreads.set_thread_sandbox_config(
            db,
            thread_id,
            enabled=True,
            config_name=name,
            reason="/setSandboxConfiguration",
        )
        _log(context, f"Sandbox configuration applied to this thread: {name}")
    except Exception as e:
        _log(context, f"/setSandboxConfiguration error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def get_sandboxing_config_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _target(context, "getSandboxingConfig")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    try:
        sb = _eggthreads.get_thread_sandbox_status(db, thread_id)
        config_lines = [
            "Current thread sandbox configuration:",
            f"  Provider: {sb.get('provider', 'unknown')}",
            f"  Enabled: {sb.get('enabled', False)}",
            f"  Available: {sb.get('available', False)}",
            f"  Effective: {sb.get('effective', False)}",
            f"  Config source: {sb.get('config_source', 'unknown')}",
            f"  Config path: {sb.get('config_path', 'unknown')}",
        ]
        warning = sb.get("warning")
        if warning:
            config_lines.append(f"  Warning: {warning}")
        config_text = "\n".join(config_lines)
        _log(context, "Sandbox configuration (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Sandbox Configuration", config_text, border_style="blue")
        else:
            _log(context, config_text)
    except Exception as e:
        _log(context, f"/getSandboxingConfig error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def register_sandbox_admin_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("toggleSandboxing", toggle_sandboxing_command, category="sandbox", usage="/toggleSandboxing", description="Toggle sandboxing for the thread subtree."))
    registry.register(CommandSpec("setSandboxConfiguration", set_sandbox_configuration_command, category="sandbox", usage="/setSandboxConfiguration <file.json>", description="Apply sandbox configuration."))
    registry.register(CommandSpec("getSandboxingConfig", get_sandboxing_config_command, category="sandbox", usage="/getSandboxingConfig", description="Show current sandbox configuration."))


@dataclass(frozen=True)
class SandboxAdminPlugin:
    name: str = "sandbox_admin"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_sandbox_admin_commands(context.command_registry)


__all__ = [
    "SANDBOX_CONFIGURATION_HELP",
    "SandboxAdminPlugin",
    "get_sandboxing_config_command",
    "register_sandbox_admin_commands",
    "set_sandbox_configuration_command",
    "toggle_sandboxing_command",
]
