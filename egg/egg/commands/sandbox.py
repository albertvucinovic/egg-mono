"""Sandbox-related command mixins for the egg application."""
from __future__ import annotations

from typing import Any, Dict, List


class SandboxCommandsMixin:
    """Mixin providing sandbox management commands."""

    def cmd_toggleSandboxing(self, arg: str) -> None:
        """Handle /toggleSandboxing command - toggle sandboxing for thread subtree."""
        # Toggle sandboxing for the *current thread subtree*.
        # Check if user sandbox control is enabled
        try:
            from eggthreads import is_user_sandbox_control_enabled  # type: ignore
            if not is_user_sandbox_control_enabled(self.db, self.current_thread):
                self.log_system("User sandbox control is disabled for this thread.")
                return
        except ImportError:
            # Older eggthreads version, assume enabled
            pass

        try:
            from eggthreads import get_thread_sandbox_status  # type: ignore

            st = get_thread_sandbox_status(self.db, self.current_thread)
            enabled_before = bool(st.get('enabled'))
            new_enabled = not enabled_before

            # Toggle only the enabled flag while keeping the current
            # effective settings. We do this by storing an explicit
            # sandbox.config event on the current thread.
            from eggthreads import get_thread_sandbox_config, set_thread_sandbox_config  # type: ignore

            cfg = get_thread_sandbox_config(self.db, self.current_thread)
            set_thread_sandbox_config(
                self.db,
                self.current_thread,
                enabled=new_enabled,
                settings=cfg.settings,
                reason='/toggleSandboxing',
            )

            st2 = get_thread_sandbox_status(self.db, self.current_thread)
            if bool(st2.get('effective')):
                self.log_system('Sandboxing ENABLED for this thread subtree.')
            else:
                warn = st2.get('warning')
                if isinstance(warn, str) and warn:
                    self.log_system(f"Sandboxing ENABLED but not effective: {warn}")
                else:
                    self.log_system('Sandboxing ENABLED but not effective.')
        except Exception as e:
            self.log_system(f'/toggleSandboxing error: {e}')

    def cmd_setSandboxConfiguration(self, arg: str) -> None:
        """Handle /setSandboxConfiguration command - apply sandbox config file."""
        # Check if user sandbox control is enabled
        try:
            from eggthreads import is_user_sandbox_control_enabled  # type: ignore
            if not is_user_sandbox_control_enabled(self.db, self.current_thread):
                self.log_system("User sandbox control is disabled for this thread.")
                return
        except ImportError:
            # Older eggthreads version, assume enabled
            pass

        name = (arg or '').strip()
        if not name:
            # Print help about sandbox configuration
            help_text = '''Sandbox Configuration and Control in Eggthreads

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
configuration for child threads (e.g., in puzzle solving or automated tasks).'''
            self.log_system('Sandbox configuration help (see console for full).')
            self.console_print_block('Sandbox Configuration', help_text.strip(), border_style='blue')
            return
        try:
            from eggthreads import set_subtree_sandbox_config  # type: ignore

            # Apply to this thread from now on. Children without
            # explicit configs will inherit it.
            from eggthreads import set_thread_sandbox_config  # type: ignore

            set_thread_sandbox_config(
                self.db,
                self.current_thread,
                enabled=True,
                config_name=name,
                reason='/setSandboxConfiguration',
            )
            self.log_system(f"Sandbox configuration applied to this thread: {name}")
        except Exception as e:
            self.log_system(f'/setSandboxConfiguration error: {e}')

    def cmd_getSandboxingConfig(self, arg: str) -> None:
        """Handle /getSandboxingConfig command - show current sandbox config."""
        try:
            from eggthreads import get_thread_sandbox_status  # type: ignore
            sb = get_thread_sandbox_status(self.db, self.current_thread)
            # Format configuration info
            config_lines = []
            config_lines.append("Current thread sandbox configuration:")
            config_lines.append(f"  Provider: {sb.get('provider', 'unknown')}")
            config_lines.append(f"  Enabled: {sb.get('enabled', False)}")
            config_lines.append(f"  Available: {sb.get('available', False)}")
            config_lines.append(f"  Effective: {sb.get('effective', False)}")
            config_lines.append(f"  Config source: {sb.get('config_source', 'unknown')}")
            config_lines.append(f"  Config path: {sb.get('config_path', 'unknown')}")
            warning = sb.get('warning')
            if warning:
                config_lines.append(f"  Warning: {warning}")
            config_text = '\n'.join(config_lines)
            self.log_system('Sandbox configuration (see console for full).')
            self.console_print_block('Sandbox Configuration', config_text, border_style='blue')
        except Exception as e:
            self.log_system(f'/getSandboxingConfig error: {e}')
