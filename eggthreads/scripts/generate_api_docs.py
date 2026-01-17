#!/usr/bin/env python3
"""Generate API.md documentation from eggthreads source code.

This script extracts function signatures and docstrings from the eggthreads
package and generates a Markdown API reference organized by conceptual
categories.

Usage:
    python scripts/generate_api_docs.py
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

# Add parent directory to path so we can import eggthreads
sys.path.insert(0, str(Path(__file__).parent.parent))

import eggthreads
from eggthreads import db, api, runner, tools_config, sandbox, tools, tool_state, token_count

# API categories organized by conceptual grouping
CATEGORIES: Dict[str, List[Tuple[str, Any]]] = {
    "Thread Lifecycle": [
        ("create_root_thread", api.create_root_thread),
        ("create_child_thread", api.create_child_thread),
        ("delete_thread", api.delete_thread),
        ("duplicate_thread", api.duplicate_thread),
        ("duplicate_thread_up_to", api.duplicate_thread_up_to),
    ],
    "Continue & Recovery": [
        ("continue_thread", api.continue_thread),
        ("continue_thread_async", api.continue_thread_async),
        ("find_continue_point", api.find_continue_point),
        ("is_thread_continuable", api.is_thread_continuable),
        ("diagnose_thread", api.diagnose_thread),
        ("ContinueResult", api.ContinueResult),
        ("ThreadDiagnosis", api.ThreadDiagnosis),
    ],
    "Messages": [
        ("append_message", api.append_message),
        ("edit_message", api.edit_message),
        ("delete_message", api.delete_message),
        ("create_snapshot", api.create_snapshot),
    ],
    "Thread Queries": [
        ("list_threads", api.list_threads),
        ("list_root_threads", api.list_root_threads),
        ("list_children_ids", api.list_children_ids),
        ("list_children_with_meta", api.list_children_with_meta),
        ("get_parent", api.get_parent),
        ("is_thread_runnable", api.is_thread_runnable),
        ("collect_subtree", api.collect_subtree),
        ("list_active_threads", api.list_active_threads),
    ],
    "Thread Control": [
        ("pause_thread", api.pause_thread),
        ("resume_thread", api.resume_thread),
        ("interrupt_thread", api.interrupt_thread),
        ("wait_subtree_idle", api.wait_subtree_idle),
    ],
    "Model Configuration": [
        ("set_thread_model", api.set_thread_model),
        ("current_thread_model", api.current_thread_model),
        ("current_thread_model_info", api.current_thread_model_info),
    ],
    "Working Directory": [
        ("get_thread_working_directory", api.get_thread_working_directory),
        ("set_thread_working_directory", api.set_thread_working_directory),
        ("set_subtree_working_directory", api.set_subtree_working_directory),
    ],
    "Tool Calls & Approval": [
        ("approve_tool_calls_for_thread", api.approve_tool_calls_for_thread),
        ("list_tool_calls_for_thread", tool_state.list_tool_calls_for_thread),
        ("list_tool_calls_for_message", tool_state.list_tool_calls_for_message),
        ("build_tool_call_states", tool_state.build_tool_call_states),
    ],
    "Tools Configuration": [
        ("ToolsConfig", tools_config.ToolsConfig),
        ("get_thread_tools_config", tools_config.get_thread_tools_config),
        ("set_thread_tools_enabled", tools_config.set_thread_tools_enabled),
        ("disable_tool_for_thread", tools_config.disable_tool_for_thread),
        ("enable_tool_for_thread", tools_config.enable_tool_for_thread),
        ("set_subtree_tools_enabled", tools_config.set_subtree_tools_enabled),
        ("disable_tool_for_subtree", tools_config.disable_tool_for_subtree),
        ("enable_tool_for_subtree", tools_config.enable_tool_for_subtree),
        ("set_thread_allow_raw_tool_output", tools_config.set_thread_allow_raw_tool_output),
    ],
    "User Commands (Bash)": [
        ("execute_bash_command", api.execute_bash_command),
        ("execute_bash_command_hidden", api.execute_bash_command_hidden),
        ("execute_bash_command_async", api.execute_bash_command_async),
        ("get_user_command_result", api.get_user_command_result),
        ("wait_for_user_command_result", api.wait_for_user_command_result),
        ("wait_for_user_command_result_async", api.wait_for_user_command_result_async),
    ],
    "Sandbox": [
        ("wrap_argv_for_sandbox", sandbox.wrap_argv_for_sandbox),
        ("wrap_argv_for_sandbox_with_config", sandbox.wrap_argv_for_sandbox_with_config),
        ("wrap_argv_for_sandbox_with_settings", sandbox.wrap_argv_for_sandbox_with_settings),
        ("get_sandbox_status", sandbox.get_sandbox_status),
        ("set_sandbox_globally_enabled", sandbox.set_sandbox_globally_enabled),
        ("set_sandbox_config", sandbox.set_sandbox_config),
        ("get_thread_sandbox_config", sandbox.get_thread_sandbox_config),
        ("set_thread_sandbox_config", sandbox.set_thread_sandbox_config),
        ("set_subtree_sandbox_config", sandbox.set_subtree_sandbox_config),
        ("get_thread_sandbox_status", sandbox.get_thread_sandbox_status),
        ("enable_user_sandbox_control", sandbox.enable_user_sandbox_control),
        ("disable_user_sandbox_control", sandbox.disable_user_sandbox_control),
        ("is_user_sandbox_control_enabled", sandbox.is_user_sandbox_control_enabled),
    ],
    "Token Statistics": [
        ("snapshot_token_stats", token_count.snapshot_token_stats),
        ("streaming_token_stats", token_count.streaming_token_stats),
        ("total_token_stats", token_count.total_token_stats),
        ("word_count_from_snapshot", api.word_count_from_snapshot),
        ("word_count_from_events", api.word_count_from_events),
    ],
    "Execution": [
        ("ThreadRunner", runner.ThreadRunner),
        ("SubtreeScheduler", runner.SubtreeScheduler),
        ("RunnerConfig", runner.RunnerConfig),
    ],
    "Database": [
        ("ThreadsDB", db.ThreadsDB),
        ("ThreadRow", db.ThreadRow),
    ],
    "Tool State & Runner Actionable": [
        ("thread_state", tool_state.thread_state),
        ("discover_runner_actionable", tool_state.discover_runner_actionable),
    ],
    "Tools Registry": [
        ("ToolRegistry", tools.ToolRegistry),
        ("create_default_tools", tools.create_default_tools),
    ],
}

# Static content sections
QUICK_START = '''
## Quick Start

```python
from eggthreads import (
    ThreadsDB,
    ThreadRunner,
    create_root_thread,
    append_message,
)

# Initialize database
db = ThreadsDB()
db.init_schema()

# Create a conversation thread
thread_id = create_root_thread(db, name="My Chat")

# Add a user message
append_message(db, thread_id, role="user", content="Hello!")

# Run the thread (requires eggllm for LLM integration)
runner = ThreadRunner(db, thread_id)
await runner.run()
```
'''

CORE_CONCEPTS = '''
## Core Concepts

### Threads and Branching

eggthreads organizes conversations as a tree of threads. Each thread maintains
its own message history and can spawn child threads for parallel exploration
or delegation.

- **Root threads**: Top-level conversations created with `create_root_thread()`
- **Child threads**: Branch conversations created with `create_child_thread()`
- **Subtrees**: All descendants of a thread, managed with `collect_subtree()`

### Event Sourcing

All state changes are recorded as immutable events in the database. This enables:

- Full audit trail of conversation history
- Snapshot rebuilding from events
- Continue/recovery from interrupted states

Key event types:
- `msg.create` - New message added
- `msg.edit` - Message content modified
- `tool_call.*` - Tool execution lifecycle
- `model.switch` - Model configuration changes
- `control.*` - Thread state control (pause, resume, interrupt)

### Tool Call Workflow

Tool calls follow a state machine:

1. **TC1**: Tool call requested by assistant
2. **TC2**: Tool call acknowledged
3. **TC3**: Approval requested (if required)
4. **TC4**: Approved/denied
5. **TC5**: Execution started
6. **TC6**: Result published

Use `build_tool_call_states()` to inspect current tool call states.

### Runner Actionable States

The `ThreadRunner` responds to three actionable states:

- **RA1**: LLM turn needed (user message awaiting response)
- **RA2**: Tool execution needed (assistant tool calls pending)
- **RA3**: User command execution (user-initiated tool calls)
'''


def get_signature(obj: Any) -> Optional[str]:
    """Extract function/class signature as a string."""
    try:
        if inspect.isclass(obj):
            # For classes, get __init__ signature
            sig = inspect.signature(obj.__init__)
            params = list(sig.parameters.values())[1:]  # Skip 'self'
            new_sig = sig.replace(parameters=params)
            return f"{obj.__name__}{new_sig}"
        elif callable(obj):
            sig = inspect.signature(obj)
            return f"{obj.__name__}{sig}"
    except (ValueError, TypeError):
        pass
    return None


def get_docstring(obj: Any) -> Optional[str]:
    """Extract docstring from object."""
    doc = inspect.getdoc(obj)
    if doc:
        return doc
    return None


def format_function(name: str, obj: Any) -> str:
    """Format a single function/class for Markdown output."""
    lines = []

    sig = get_signature(obj)
    if sig:
        lines.append(f"### `{sig}`")
    else:
        lines.append(f"### `{name}`")

    lines.append("")

    doc = get_docstring(obj)
    if doc:
        lines.append(doc)
    else:
        lines.append("*No documentation available.*")

    lines.append("")
    return "\n".join(lines)


def format_dataclass(name: str, obj: Type) -> str:
    """Format a dataclass for Markdown output."""
    import dataclasses

    lines = []
    lines.append(f"### `{name}`")
    lines.append("")

    doc = get_docstring(obj)
    if doc:
        lines.append(doc)
        lines.append("")

    # Extract fields
    if hasattr(obj, "__dataclass_fields__"):
        lines.append("**Fields:**")
        lines.append("")
        for field_name, field_info in obj.__dataclass_fields__.items():
            field_type = field_info.type
            if hasattr(field_type, "__name__"):
                type_str = field_type.__name__
            else:
                type_str = str(field_type)

            # Check if field has a real default value
            has_default = (
                field_info.default is not dataclasses.MISSING
                and field_info.default is not None
            )
            has_default_factory = field_info.default_factory is not dataclasses.MISSING

            if has_default:
                lines.append(f"- `{field_name}`: `{type_str}` = `{field_info.default!r}`")
            elif has_default_factory:
                # Show that there's a factory, but don't execute it
                lines.append(f"- `{field_name}`: `{type_str}` (default factory)")
            else:
                # Required field (no default)
                lines.append(f"- `{field_name}`: `{type_str}` (required)")
        lines.append("")

    return "\n".join(lines)


def generate_api_reference() -> str:
    """Generate the complete API reference content."""
    lines = []

    # Header
    lines.append("# eggthreads API Reference")
    lines.append("")
    lines.append("This document provides a comprehensive API reference for the eggthreads library.")
    lines.append("")

    # Quick Start
    lines.append(QUICK_START.strip())
    lines.append("")

    # Core Concepts
    lines.append(CORE_CONCEPTS.strip())
    lines.append("")

    # Table of Contents
    lines.append("## Table of Contents")
    lines.append("")
    for category in CATEGORIES:
        anchor = category.lower().replace(" ", "-").replace("&", "").replace("(", "").replace(")", "")
        lines.append(f"- [{category}](#{anchor})")
    lines.append("")

    # API Reference by category
    lines.append("---")
    lines.append("")

    for category, items in CATEGORIES.items():
        lines.append(f"## {category}")
        lines.append("")

        for name, obj in items:
            if inspect.isclass(obj) and hasattr(obj, "__dataclass_fields__"):
                lines.append(format_dataclass(name, obj))
            else:
                lines.append(format_function(name, obj))

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main():
    """Generate API.md and write to file."""
    output_path = Path(__file__).parent.parent / "API.md"

    content = generate_api_reference()

    output_path.write_text(content, encoding="utf-8")
    print(f"Generated {output_path}")

    # Print statistics
    total_items = sum(len(items) for items in CATEGORIES.values())
    documented = 0
    undocumented = []

    for category, items in CATEGORIES.items():
        for name, obj in items:
            doc = get_docstring(obj)
            if doc:
                documented += 1
            else:
                undocumented.append(f"{category}: {name}")

    print(f"\nTotal API items: {total_items}")
    print(f"Documented: {documented}")
    print(f"Missing docstrings: {len(undocumented)}")

    if undocumented:
        print("\nUndocumented items:")
        for item in undocumented:
            print(f"  - {item}")


if __name__ == "__main__":
    main()
