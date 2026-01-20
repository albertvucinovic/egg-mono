"""eggthreads - Tree-structured conversation threads with event sourcing.

eggthreads provides infrastructure for managing AI conversations as a tree
of threads, where each thread maintains its own message history and can
spawn child threads for parallel exploration or task delegation.

Key features:
- **Thread management**: Create root and child threads, branch conversations
- **Event sourcing**: All state changes recorded as immutable events
- **Tool call workflow**: TC1-TC6 state machine with approval gates
- **Runner actionables**: RA1 (LLM turns), RA2 (tool execution), RA3 (user commands)
- **Sandbox execution**: Isolated command execution with configurable policies

Quick start::

    from eggthreads import ThreadsDB, create_root_thread, append_message

    db = ThreadsDB()
    db.init_schema()

    thread_id = create_root_thread(db, name="My Chat")
    append_message(db, thread_id, role="user", content="Hello!")

See API.md for comprehensive documentation.
"""
from .db import ThreadsDB  # type: ignore
from .runner import SubtreeScheduler, ThreadRunner, RunnerConfig, set_default_tool_timeout, get_default_tool_timeout  # type: ignore
from .snapshot import SnapshotBuilder  # type: ignore
from .api import (
    create_root_thread,
    create_child_thread,
    append_message,
    edit_message,
    delete_message,
    delete_thread,
    is_thread_runnable,
    get_thread_status,
    get_thread_statuses_bulk,
    list_threads,
    list_root_threads,
    get_parent,
    list_children_with_meta,
    list_children_ids,
    current_open_invoke,
    create_snapshot,
    interrupt_thread,
    pause_thread,
    resume_thread,
    set_thread_model,
    current_thread_model,
    current_thread_model_info,
    duplicate_thread,
    duplicate_thread_up_to,
    get_thread_working_directory,
    set_thread_working_directory,
    collect_subtree,
    list_active_threads,
    wait_subtree_idle,
    word_count_from_snapshot,
    word_count_from_events,
    set_subtree_working_directory,
    approve_tool_calls_for_thread,
    execute_bash_command,
    execute_bash_command_hidden,
    get_user_command_result,
    wait_for_user_command_result,
    wait_for_user_command_result_async,
    execute_bash_command_async,
    # Context limit API
    set_context_limit,
    get_context_limit,
    # Continue thread API
    ContinueResult,
    continue_thread,
    continue_thread_async,
    find_continue_point,
    is_thread_continuable,
    # Thread diagnosis
    ThreadDiagnosis,
    diagnose_thread,
    # Thread scheduling API
    UNSET,
    ThreadSchedulingSettings,
    get_thread_scheduling,
    set_thread_scheduling,
)  # type: ignore
from .arg_parser import parse_args, ParsedArgs  # type: ignore
from .tool_state import (
    list_tool_calls_for_thread,
    list_tool_calls_for_message,
    build_tool_call_states,
    thread_state,
    discover_runner_actionable,
)

from .token_count import (
    snapshot_token_stats,
    streaming_token_stats,
    total_token_stats,
)
from .tools_config import (
    ToolsConfig,
    get_thread_tools_config,
    set_thread_tools_enabled,
    disable_tool_for_thread,
    enable_tool_for_thread,
    set_subtree_tools_enabled,
    disable_tool_for_subtree,
    enable_tool_for_subtree,
    set_thread_allow_raw_tool_output,
)
from .sandbox import (
    wrap_argv_for_sandbox,
    wrap_argv_for_sandbox_with_config,
    wrap_argv_for_sandbox_with_settings,
    get_sandbox_status,
    set_sandbox_globally_enabled,
    set_sandbox_config,
    get_thread_sandbox_config,
    set_thread_sandbox_config,
    set_subtree_sandbox_config,
    get_thread_sandbox_status,
    enable_user_sandbox_control,
    disable_user_sandbox_control,
    is_user_sandbox_control_enabled,
)

from .tools import ToolRegistry, create_default_tools

from .llm import create_llm_client

__all__ = [
    'ThreadsDB', 'SubtreeScheduler', 'ThreadRunner', 'RunnerConfig', 'SnapshotBuilder',
    'set_default_tool_timeout', 'get_default_tool_timeout',
    'create_root_thread', 'create_child_thread', 'append_message', 'edit_message', 'delete_message', 'delete_thread', 'is_thread_runnable', 'get_thread_status', 'get_thread_statuses_bulk',
    'list_threads', 'list_root_threads', 'get_parent', 'list_children_with_meta', 'list_children_ids', 'current_open_invoke',
    'current_thread_model', 'current_thread_model_info', 'duplicate_thread', 'duplicate_thread_up_to',
    'collect_subtree', 'list_active_threads', 'wait_subtree_idle',
    'word_count_from_snapshot', 'word_count_from_events',
    'set_subtree_working_directory',
    'approve_tool_calls_for_thread',
    'execute_bash_command', 'execute_bash_command_hidden', 'get_user_command_result', 'wait_for_user_command_result',
    'wait_for_user_command_result_async',
    'execute_bash_command_async',
    # Context limit API
    'set_context_limit', 'get_context_limit',
    # Continue thread API
    'ContinueResult', 'continue_thread', 'continue_thread_async', 'find_continue_point', 'is_thread_continuable',
    # Thread diagnosis
    'ThreadDiagnosis', 'diagnose_thread',
    # Thread scheduling API
    'UNSET', 'ThreadSchedulingSettings', 'get_thread_scheduling', 'set_thread_scheduling',
    # Argument parser
    'parse_args', 'ParsedArgs',
    'list_tool_calls_for_thread', 'list_tool_calls_for_message', 'build_tool_call_states', 'thread_state',
    'discover_runner_actionable',
    'ToolsConfig', 'get_thread_tools_config', 'set_thread_tools_enabled', 'disable_tool_for_thread', 'enable_tool_for_thread',
    'set_subtree_tools_enabled', 'disable_tool_for_subtree', 'enable_tool_for_subtree',
    'set_thread_allow_raw_tool_output',
    'wrap_argv_for_sandbox', 'wrap_argv_for_sandbox_with_config',
    'wrap_argv_for_sandbox_with_settings', 'get_sandbox_status',
    'set_sandbox_globally_enabled', 'set_sandbox_config',
    'get_thread_sandbox_config', 'set_thread_sandbox_config', 'set_subtree_sandbox_config', 'get_thread_sandbox_status',
    'ToolRegistry', 'create_default_tools',
    'create_llm_client',
    'snapshot_token_stats', 'streaming_token_stats', 'total_token_stats', 'EventWatcher',
    'enable_user_sandbox_control',
    'disable_user_sandbox_control',
    'is_user_sandbox_control_enabled',
]
from .event_watcher import EventWatcher
