"""Sandbox management commands for eggw backend."""
from __future__ import annotations

from eggthreads import (
    get_thread_sandbox_status,
    get_thread_sandbox_config,
    set_thread_sandbox_config,
    is_user_sandbox_control_enabled,
)

from ..models import CommandResponse
from .. import core


async def cmd_toggle_sandboxing(thread_id: str) -> CommandResponse:
    """Handle /toggleSandboxing command - toggle sandboxing for thread subtree."""
    # Check if user sandbox control is enabled
    try:
        if not is_user_sandbox_control_enabled(core.db, thread_id):
            return CommandResponse(
                success=False,
                message="User sandbox control is disabled for this thread.",
            )
    except Exception:
        pass  # Older version, assume enabled

    try:
        st = get_thread_sandbox_status(core.db, thread_id)
        enabled_before = bool(st.get('enabled'))
        new_enabled = not enabled_before

        # Toggle only the enabled flag while keeping current settings
        cfg = get_thread_sandbox_config(core.db, thread_id)
        set_thread_sandbox_config(
            core.db,
            thread_id,
            enabled=new_enabled,
            settings=cfg.settings,
            reason='/toggleSandboxing',
        )

        st2 = get_thread_sandbox_status(core.db, thread_id)
        effective = bool(st2.get('effective'))
        warning = st2.get('warning')

        if effective:
            return CommandResponse(
                success=True,
                message='Sandboxing ENABLED for this thread subtree.',
                data={"enabled": True, "effective": True},
            )
        elif new_enabled:
            msg = 'Sandboxing ENABLED but not effective'
            if warning:
                msg += f": {warning}"
            return CommandResponse(
                success=True,
                message=msg,
                data={"enabled": True, "effective": False, "warning": warning},
            )
        else:
            return CommandResponse(
                success=True,
                message='Sandboxing DISABLED for this thread subtree.',
                data={"enabled": False, "effective": False},
            )
    except Exception as e:
        return CommandResponse(success=False, message=f'/toggleSandboxing error: {e}')


async def cmd_set_sandbox_configuration(thread_id: str, config_name: str) -> CommandResponse:
    """Handle /setSandboxConfiguration command - apply sandbox config file."""
    # Check if user sandbox control is enabled
    try:
        if not is_user_sandbox_control_enabled(core.db, thread_id):
            return CommandResponse(
                success=False,
                message="User sandbox control is disabled for this thread.",
            )
    except Exception:
        pass  # Older version, assume enabled

    name = config_name.strip()
    if not name:
        # Return help message
        return CommandResponse(
            success=True,
            message="""Sandbox Configuration Commands:
  /toggleSandboxing - Toggle sandbox on/off for this thread
  /setSandboxConfiguration <file.json> - Apply config from .egg/sandbox/
  /getSandboxingConfig - Show current sandbox configuration

Config files are stored in .egg/sandbox/ directory.
Use tab completion to see available configs.""",
        )

    try:
        set_thread_sandbox_config(
            core.db,
            thread_id,
            enabled=True,
            config_name=name,
            reason='/setSandboxConfiguration',
        )
        return CommandResponse(
            success=True,
            message=f"Sandbox configuration applied: {name}",
            data={"config_name": name},
        )
    except Exception as e:
        return CommandResponse(success=False, message=f'/setSandboxConfiguration error: {e}')


async def cmd_get_sandboxing_config(thread_id: str) -> CommandResponse:
    """Handle /getSandboxingConfig command - show current sandbox config."""
    try:
        sb = get_thread_sandbox_status(core.db, thread_id)
        lines = [
            "Current thread sandbox configuration:",
            f"  Provider: {sb.get('provider', 'unknown')}",
            f"  Enabled: {sb.get('enabled', False)}",
            f"  Available: {sb.get('available', False)}",
            f"  Effective: {sb.get('effective', False)}",
            f"  Config source: {sb.get('config_source', 'unknown')}",
        ]
        config_path = sb.get('config_path')
        if config_path:
            lines.append(f"  Config path: {config_path}")
        warning = sb.get('warning')
        if warning:
            lines.append(f"  Warning: {warning}")

        return CommandResponse(
            success=True,
            message="\n".join(lines),
            data={
                "provider": sb.get('provider'),
                "enabled": sb.get('enabled'),
                "available": sb.get('available'),
                "effective": sb.get('effective'),
                "warning": warning,
            },
        )
    except Exception as e:
        return CommandResponse(success=False, message=f'/getSandboxingConfig error: {e}')
