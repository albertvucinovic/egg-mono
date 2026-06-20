"""Command handlers for eggw backend."""
from eggthreads import append_message, approve_tool_calls_for_thread, create_snapshot
from eggthreads.command_catalog import CommandContext, create_default_command_registry

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
from .session import (
    cmd_session_status,
    cmd_session_on,
    cmd_session_off,
    cmd_session_stop,
    cmd_session_reset,
    cmd_session_cleanup,
    cmd_python_repl,
    cmd_bash_repl,
)
from .auth import cmd_login, cmd_logout, cmd_auth_status
from .compaction import cmd_compact, cmd_compact_with_summary, cmd_context, cmd_set_auto_compact_threshold
from .attachments import (
    cmd_attach,
    cmd_attach_output,
    cmd_attachments,
    cmd_clear_attachments,
    cmd_save_provider_artifact,
)
from .image_generation import cmd_image_generate
from .utility import (
    cmd_toggle_auto_approval,
    cmd_toggle_auto_continue_on_error,
    cmd_cost,
    cmd_schedulers,
    cmd_wait_for_threads,
    execute_bash_command_handler,
    cmd_help,
    cmd_skills,
    cmd_skill,
    cmd_toggle_panel,
    cmd_paste,
    cmd_enter_mode,
    cmd_toggle_borders,
    cmd_display_verbosity,
    cmd_quit,
    cmd_reload,
    cmd_theme,
    cmd_start_searxng,
    cmd_stop_searxng,
    get_auto_approval_status,
    cmd_setContextLimit,
    cmd_setThreadPriority,
)

from ..models import CommandResponse
from .. import core
from ..core import ensure_scheduler_for

_SHARED_COMMAND_ADAPTER_COMMANDS = {"btw", "waitForThreads"}


def _execute_shared_command(thread_id: str, command_name: str, command_arg: str) -> CommandResponse:
    """Execute selected shared CommandRegistry handlers for EggW."""
    if command_name not in _SHARED_COMMAND_ADAPTER_COMMANDS:
        return CommandResponse(success=False, message=f"Unsupported shared command adapter: /{command_name}")

    result = create_default_command_registry().execute(
        command_name,
        CommandContext(
            db=core.db,
            current_thread=thread_id,
            start_scheduler=ensure_scheduler_for,
            append_message=append_message,
            approve_tool_calls=approve_tool_calls_for_thread,
            create_snapshot=create_snapshot,
        ),
        command_arg,
    )
    data = {"start_schedulers": list(result.start_schedulers)} if result.start_schedulers else None
    return CommandResponse(
        success=bool(result.clear_input),
        message=result.message or f"/{command_name} completed.",
        data=data,
    )

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
    # Session commands
    "cmd_session_status",
    "cmd_session_on",
    "cmd_session_off",
    "cmd_session_stop",
    "cmd_session_reset",
    "cmd_session_cleanup",
    "cmd_python_repl",
    "cmd_bash_repl",
    # Utility commands
    "cmd_toggle_auto_approval",
    "cmd_toggle_auto_continue_on_error",
    "cmd_cost",
    "cmd_schedulers",
    "cmd_wait_for_threads",
    "execute_bash_command_handler",
    "cmd_help",
    "cmd_skills",
    "cmd_skill",
    "cmd_toggle_panel",
    "cmd_paste",
    "cmd_enter_mode",
    "cmd_toggle_borders",
    "cmd_display_verbosity",
    "cmd_quit",
    "cmd_reload",
    "cmd_theme",
    "cmd_start_searxng",
    "cmd_stop_searxng",
    "get_auto_approval_status",
    "cmd_setContextLimit",
    "cmd_setThreadPriority",
    # Attachment commands
    "cmd_attach",
    "cmd_attachments",
    "cmd_attach_output",
    "cmd_clear_attachments",
    "cmd_save_provider_artifact",
    # Image generation commands
    "cmd_image_generate",
    # Auth commands
    "cmd_login",
    "cmd_logout",
    "cmd_auth_status",
    # Compaction commands
    "cmd_compact",
    "cmd_compact_with_summary",
    "cmd_context",
    "cmd_set_auto_compact_threshold",
    # Dispatcher
    "dispatch_command",
]


async def dispatch_command(thread_id: str, command: str, *, staged_attachments=None) -> CommandResponse:
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
        elif command_name == "spawnChildThread":
            return await cmd_spawn(thread_id, command_arg)
        elif command_name == "newThread":
            return await cmd_new_thread(command_arg)
        elif command_name == "help":
            return cmd_help()
        elif command_name == "btw":
            return _execute_shared_command(thread_id, command_name, command_arg)
        elif command_name == "skills":
            return await cmd_skills(thread_id, command_arg)
        elif command_name == "skill":
            return await cmd_skill(thread_id, command_arg)
        elif command_name == "toggleAutoApproval":
            return await cmd_toggle_auto_approval(thread_id)
        elif command_name == "toggleAutoContinueOnError":
            return await cmd_toggle_auto_continue_on_error(thread_id, command_arg)
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
        elif command_name == "compact":
            return await cmd_compact(thread_id, command_arg)
        elif command_name == "compactWithSummary":
            return await cmd_compact_with_summary(thread_id)
        elif command_name == "context":
            return await cmd_context(thread_id)
        elif command_name == "setAutoCompactThreshold":
            return await cmd_set_auto_compact_threshold(thread_id, command_arg)
        elif command_name == "rename":
            return await cmd_rename(thread_id, command_arg)
        elif command_name == "cost":
            return await cmd_cost(thread_id)
        elif command_name == "attach":
            return await cmd_attach(thread_id, command_arg)
        elif command_name == "attachments":
            return cmd_attachments(staged_attachments)
        elif command_name == "attachOutput":
            return await cmd_attach_output(thread_id, command_arg)
        elif command_name == "clearAttachments":
            return cmd_clear_attachments(staged_attachments)
        elif command_name in {"saveProviderArtifact", "saveProviderOutput"}:
            return await cmd_save_provider_artifact(thread_id, command_arg)
        elif command_name == "imageGenerate":
            return await cmd_image_generate(thread_id, command_arg)
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
        elif command_name == "sessionStatus":
            return await cmd_session_status(thread_id)
        elif command_name == "sessionOn":
            return await cmd_session_on(thread_id, command_arg)
        elif command_name == "sessionOff":
            return await cmd_session_off(thread_id)
        elif command_name == "sessionStop":
            return await cmd_session_stop(thread_id, command_arg)
        elif command_name == "sessionReset":
            return await cmd_session_reset(thread_id, command_arg)
        elif command_name == "sessionCleanup":
            return await cmd_session_cleanup(thread_id, command_arg)
        elif command_name == "pythonRepl":
            return await cmd_python_repl(thread_id, command_arg)
        elif command_name == "bashRepl":
            return await cmd_bash_repl(thread_id, command_arg)
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
            return _execute_shared_command(thread_id, command_name, command_arg)
        elif command_name == "togglePanel":
            return cmd_toggle_panel(command_arg)
        # P3 Commands
        elif command_name == "paste":
            return cmd_paste()
        elif command_name == "enterMode":
            return cmd_enter_mode(command_arg)
        elif command_name == "toggleBorders":
            return cmd_toggle_borders()
        elif command_name == "displayVerbosity":
            return cmd_display_verbosity(command_arg)
        elif command_name == "theme":
            return cmd_theme(command_arg)
        elif command_name == "quit":
            return cmd_quit()
        elif command_name == "reload":
            return cmd_reload(thread_id)
        elif command_name == "redraw":
            return CommandResponse(success=True, message="Redraw is not needed in eggw; the browser UI updates automatically.")
        elif command_name == "displayMode":
            return CommandResponse(success=True, message="/displayMode is terminal-only; eggw uses the browser layout.")
        elif command_name == "startSearxng":
            return cmd_start_searxng()
        elif command_name == "stopSearxng":
            return cmd_stop_searxng()
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
