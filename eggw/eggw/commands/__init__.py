"""Command handlers for eggw backend."""
from .thread import (
    cmd_spawn,
    cmd_new_thread,
    cmd_parent_thread,
    cmd_switch_thread,
    cmd_list_threads,
    cmd_list_children,
    cmd_delete_thread,
    cmd_duplicate_thread,
    cmd_continue,
    cmd_rename,
    cmd_spawn_auto_approved,
)
from .model import cmd_model, cmd_update_all_models
from .tools import (
    cmd_tools_on,
    cmd_tools_off,
    cmd_tools_status,
    cmd_disable_tool,
    cmd_enable_tool,
    cmd_tools_secrets,
    cmd_tool_info,
)
from .sandbox import (
    cmd_toggle_sandboxing,
    cmd_set_sandbox_configuration,
    cmd_get_sandboxing_config,
)
from .auth import cmd_login, cmd_logout, cmd_auth_status
from .utility import (
    cmd_toggle_auto_approval,
    cmd_cost,
    cmd_schedulers,
    cmd_wait_for_threads,
    execute_bash_command_handler,
    cmd_help,
    cmd_toggle_panel,
    cmd_paste,
    cmd_enter_mode,
    cmd_toggle_borders,
    cmd_quit,
    cmd_theme,
    get_auto_approval_status,
    cmd_setContextLimit,
    cmd_setThreadPriority,
)

from models import CommandResponse

__all__ = [
    # Thread commands
    "cmd_spawn",
    "cmd_new_thread",
    "cmd_parent_thread",
    "cmd_switch_thread",
    "cmd_list_threads",
    "cmd_list_children",
    "cmd_delete_thread",
    "cmd_duplicate_thread",
    "cmd_continue",
    "cmd_rename",
    "cmd_spawn_auto_approved",
    # Model commands
    "cmd_model",
    "cmd_update_all_models",
    # Tools commands
    "cmd_tools_on",
    "cmd_tools_off",
    "cmd_tools_status",
    "cmd_disable_tool",
    "cmd_enable_tool",
    "cmd_tools_secrets",
    "cmd_tool_info",
    # Sandbox commands
    "cmd_toggle_sandboxing",
    "cmd_set_sandbox_configuration",
    "cmd_get_sandboxing_config",
    # Utility commands
    "cmd_toggle_auto_approval",
    "cmd_cost",
    "cmd_schedulers",
    "cmd_wait_for_threads",
    "execute_bash_command_handler",
    "cmd_help",
    "cmd_toggle_panel",
    "cmd_paste",
    "cmd_enter_mode",
    "cmd_toggle_borders",
    "cmd_quit",
    "cmd_theme",
    "get_auto_approval_status",
    "cmd_setContextLimit",
    "cmd_setThreadPriority",
    # Auth commands
    "cmd_login",
    "cmd_logout",
    "cmd_auth_status",
    # Dispatcher
    "dispatch_command",
]


async def dispatch_command(thread_id: str, command: str) -> CommandResponse:
    """Dispatch a slash command to the appropriate handler.

    Returns CommandResponse for the command result.
    """
    cmd = command.strip()

    # Handle shell commands: $$ (hidden) or $ (visible)
    if cmd.startswith('$$') and len(cmd) > 2:
        return await execute_bash_command_handler(thread_id, cmd[2:].strip(), hidden=True)
    elif cmd.startswith('$') and len(cmd) > 1:
        return await execute_bash_command_handler(thread_id, cmd[1:].strip(), hidden=False)

    # Handle slash commands
    if cmd.startswith('/'):
        parts = cmd[1:].split(None, 1)
        command_name = parts[0] if parts else ""
        command_arg = parts[1] if len(parts) > 1 else ""

        # Dispatch to command handlers
        if command_name == "model":
            return await cmd_model(thread_id, command_arg)
        elif command_name == "spawn" or command_name == "spawnChildThread":
            return await cmd_spawn(thread_id, command_arg)
        elif command_name == "newThread":
            return await cmd_new_thread(command_arg)
        elif command_name == "help":
            return cmd_help()
        elif command_name == "toggleAutoApproval":
            return await cmd_toggle_auto_approval(thread_id)
        elif command_name == "parentThread":
            return await cmd_parent_thread(thread_id)
        elif command_name == "thread":
            return await cmd_switch_thread(command_arg)
        elif command_name == "threads":
            return await cmd_list_threads()
        elif command_name == "listChildren":
            return await cmd_list_children(thread_id)
        elif command_name == "deleteThread":
            return await cmd_delete_thread(thread_id, command_arg)
        elif command_name == "duplicateThread":
            return await cmd_duplicate_thread(thread_id, command_arg)
        elif command_name == "continue":
            return await cmd_continue(thread_id, command_arg)
        elif command_name == "rename":
            return await cmd_rename(thread_id, command_arg)
        elif command_name == "cost":
            return await cmd_cost(thread_id)
        elif command_name == "toolsOn":
            return await cmd_tools_on(thread_id)
        elif command_name == "toolsOff":
            return await cmd_tools_off(thread_id)
        elif command_name == "toolsStatus":
            return await cmd_tools_status(thread_id)
        elif command_name == "schedulers":
            return cmd_schedulers()
        elif command_name == "toggleSandboxing":
            return await cmd_toggle_sandboxing(thread_id)
        elif command_name == "setSandboxConfiguration":
            return await cmd_set_sandbox_configuration(thread_id, command_arg)
        elif command_name == "getSandboxingConfig":
            return await cmd_get_sandboxing_config(thread_id)
        # P1 Commands
        elif command_name == "updateAllModels":
            return await cmd_update_all_models(command_arg)
        elif command_name == "disableTool":
            return await cmd_disable_tool(thread_id, command_arg)
        elif command_name == "enableTool":
            return await cmd_enable_tool(thread_id, command_arg)
        elif command_name == "toolInfo":
            return await cmd_tool_info(thread_id, command_arg)
        elif command_name == "spawnAutoApprovedChildThread":
            return await cmd_spawn_auto_approved(thread_id, command_arg)
        # P2 Commands
        elif command_name == "toolsSecrets":
            return await cmd_tools_secrets(thread_id, command_arg)
        elif command_name == "waitForThreads":
            return await cmd_wait_for_threads(thread_id, command_arg)
        elif command_name == "togglePanel":
            return cmd_toggle_panel(command_arg)
        # P3 Commands
        elif command_name == "paste":
            return cmd_paste()
        elif command_name == "enterMode":
            return cmd_enter_mode(command_arg)
        elif command_name == "toggleBorders":
            return cmd_toggle_borders()
        elif command_name == "theme":
            return cmd_theme(command_arg)
        elif command_name == "quit":
            return cmd_quit()
        elif command_name == "setContextLimit":
            return await cmd_setContextLimit(thread_id, command_arg)
        elif command_name == "setThreadPriority":
            return await cmd_setThreadPriority(thread_id, command_arg)
        # Auth commands
        elif command_name == "login":
            return await cmd_login(thread_id)
        elif command_name == "logout":
            return await cmd_logout(thread_id)
        elif command_name == "authStatus":
            return await cmd_auth_status(thread_id)
        else:
            return CommandResponse(
                success=False,
                message=f"Unknown command: /{command_name}",
            )

    return CommandResponse(success=False, message="Invalid command format")
