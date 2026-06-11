from __future__ import annotations

"""Built-in subagent/thread orchestration tools."""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

from ..plugins import PluginContext
from ..tools import ToolRegistry, resolve_tool_timeout_arg


def clean_optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value
    return None


def clean_bool_arg(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def tool_names_from_arg(value: Any) -> list[str]:
    if isinstance(value, str):
        # Accept comma/whitespace separated strings for local callers.
        return [p for p in re.split(r"[\s,]+", value) if p]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]
    return []


def spawn_parent_id(args: Dict[str, Any]) -> str:
    # Direct/local callers provide parent_thread_id explicitly.
    # Model-initiated calls inherit the current thread via _thread_id, which
    # ToolRegistry.execute injects from runner context.
    return (args.get("parent_thread_id") or args.get("_thread_id") or "").strip()


def spawn_initial_model_key(args: Dict[str, Any]) -> str | None:
    # The model-facing spawn tools no longer expose model selection.
    # Model-initiated calls therefore inherit from the parent thread.
    # Direct/local callers that explicitly pass parent_thread_id may override.
    if "parent_thread_id" not in args:
        return None
    return clean_optional_text(args.get("initial_model_key"))


def apply_spawn_child_configuration(args: Dict[str, Any], parent_id: str, child: str) -> None:
    """Apply attenuated tool/session config requested by spawn args."""

    from ..db import ThreadsDB
    from ..session import get_thread_session_config, set_thread_session_config
    from ..tools_config import disable_tool_for_thread, get_thread_tools_config, set_thread_tool_allowlist

    db = ThreadsDB()

    # Tool capability attenuation: create_child_thread has already copied the
    # parent's effective tools configuration by value. A requested child
    # allowlist can only narrow that inherited capability set.
    parent_cfg = get_thread_tools_config(db, parent_id)
    requested_allowed = tool_names_from_arg(args.get("allowed_tools"))
    if requested_allowed:
        allowed = sorted({name for name in requested_allowed if parent_cfg.is_tool_allowed(name)})
        set_thread_tool_allowlist(db, child, allowed)

    for name in tool_names_from_arg(args.get("disabled_tools")):
        disable_tool_for_thread(db, child, name)

    # Optional explicit session sharing. If share_session is omitted, honour
    # the parent's share_with_children_default policy.
    parent_session = get_thread_session_config(db, parent_id)
    share_arg = args.get("share_session")
    share_requested = clean_bool_arg(share_arg) if share_arg is not None else bool(parent_session.share_with_children_default)
    if share_requested and parent_session.enabled and parent_session.session_id:
        set_thread_session_config(
            db,
            child,
            enabled=True,
            provider=parent_session.provider,
            image=parent_session.image,
            share="session",
            session_id=parent_session.session_id,
            owner_thread_id=parent_session.owner_thread_id or parent_id,
            workspace=parent_session.workspace,
            network=parent_session.network,
            share_with_children_default=parent_session.share_with_children_default,
            share_repl=clean_bool_arg(args.get("share_repl")) if args.get("share_repl") is not None else parent_session.share_repl,
            reason="spawn_agent share_session",
        )


def inherited_system_prompt(db: Any, parent_id: str, explicit: str | None) -> str:
    """Resolve explicit or inherited system prompt for spawned children."""

    if explicit:
        return explicit

    from ..api import create_snapshot

    system_prompt = None
    try:
        row = db.get_thread(parent_id)
        # Build a fresh snapshot if needed so we see system messages even for
        # recently created parents.
        if row and not row.snapshot_json:
            try:
                create_snapshot(db, parent_id)
                row = db.get_thread(parent_id)
            except Exception:
                pass
        if row and row.snapshot_json:
            try:
                snap = json.loads(row.snapshot_json)
                msgs = snap.get("messages", []) or []
                for m in msgs:
                    try:
                        if m.get("role") == "system" and isinstance(m.get("content"), str):
                            system_prompt = m.get("content") or None
                            if system_prompt:
                                break
                    except Exception:
                        continue
            except Exception:
                pass
    except Exception:
        pass
    return system_prompt or "You are a helpful assistant."


def spawn_agent_tool(args: Dict[str, Any]) -> str:
    from ..api import append_message, create_child_thread, create_snapshot
    from ..db import ThreadsDB

    parent_id = spawn_parent_id(args)
    if not parent_id:
        return "Error: parent_thread_id is required."

    label = (args.get("label") or "spawn").strip() or "spawn"
    user_text = (args.get("context_text") or "").strip() or "Spawned task"
    initial_model_key = spawn_initial_model_key(args)
    explicit_system_prompt = (args.get("system_prompt") or "").strip() or None

    db = ThreadsDB()
    system_prompt = inherited_system_prompt(db, parent_id, explicit_system_prompt)

    # Presentation-only REPL bridge hint; ThreadRunner reads it from persisted
    # tool-call arguments before invoking this implementation.
    args.pop("_egg_raw_thread_id_result", None)

    try:
        child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
    except Exception as e:
        return f"Error: failed to create child thread: {e}"

    # Sandbox configuration inheritance is handled by api.create_child_thread.
    try:
        apply_spawn_child_configuration(args, parent_id, child)
    except Exception:
        # Child creation should not fail solely because optional attenuation or
        # session propagation failed. Conservative defaults still apply through
        # normal thread inheritance.
        pass

    try:
        append_message(db, child, "system", system_prompt)
    except Exception:
        pass

    try:
        append_message(db, child, "user", user_text)
    except Exception as e:
        return f"Error: created child {child} but failed to append user message: {e}"

    try:
        create_snapshot(db, child)
    except Exception:
        pass

    return child


def spawn_agent_auto_tool(args: Dict[str, Any]) -> str:
    from ..api import append_message, create_child_thread, create_snapshot
    from ..db import ThreadsDB

    parent_id = spawn_parent_id(args)
    if not parent_id:
        return "Error: parent_thread_id is required."

    label = (args.get("label") or "spawn_auto").strip() or "spawn_auto"
    user_text = (args.get("context_text") or "").strip() or "Spawned task"
    initial_model_key = spawn_initial_model_key(args)
    explicit_system_prompt = (args.get("system_prompt") or "").strip() or None

    args.pop("_egg_raw_thread_id_result", None)

    db = ThreadsDB()
    system_prompt = inherited_system_prompt(db, parent_id, explicit_system_prompt)

    try:
        child = create_child_thread(db, parent_id, name=label, initial_model_key=initial_model_key)
    except Exception as e:
        return f"Error: failed to create child thread: {e}"

    try:
        apply_spawn_child_configuration(args, parent_id, child)
    except Exception:
        pass

    try:
        append_message(db, child, "system", system_prompt)
    except Exception:
        pass
    try:
        append_message(db, child, "user", user_text)
    except Exception as e:
        return f"Error: created child {child} but failed to append user message: {e}"

    try:
        db.append_event(
            event_id=os.urandom(10).hex(),
            thread_id=child,
            type_="tool_call.approval",
            msg_id=None,
            invoke_id=None,
            payload={
                "decision": "global_approval",
                "reason": "spawn_agent_auto enabled global tool auto-approval for this thread",
            },
        )
    except Exception:
        pass

    try:
        create_snapshot(db, child)
    except Exception:
        pass

    return child


def send_message_to_child_tool(args: Dict[str, Any]) -> str:
    from ..api import send_message_to_child_thread
    from ..db import ThreadsDB

    manager_id = (args.get("_thread_id") or args.get("manager_thread_id") or "").strip()
    child_id = (args.get("child_thread_id") or args.get("thread_id") or "").strip()
    message = str(args.get("message") or args.get("context_text") or "")
    wait_raw = args.get("require_idle")
    require_idle = True if wait_raw is None else clean_bool_arg(wait_raw)
    try:
        msg_id = send_message_to_child_thread(
            ThreadsDB(),
            manager_id,
            child_id,
            message,
            require_idle=require_idle,
        )
    except Exception as e:
        return f"Error: {e}"
    return f"Sent message {msg_id[-8:]} to child thread {child_id[-8:]}. Use wait to read its response."


def continue_subthread_tool(args: Dict[str, Any]) -> str:
    from ..api import continue_child_thread
    from ..db import ThreadsDB

    manager_id = (args.get("_thread_id") or args.get("manager_thread_id") or "").strip()
    child_id = (args.get("child_thread_id") or args.get("thread_id") or "").strip()
    msg_id = clean_optional_text(args.get("msg_id"))
    result = continue_child_thread(ThreadsDB(), manager_id, child_id, msg_id=msg_id)
    payload = {
        "success": result.success,
        "thread_id": child_id,
        "continue_from_msg_id": result.continue_from_msg_id,
        "skipped_msg_ids": result.skipped_msg_ids,
        "message": result.message,
    }
    if result.diagnosis is not None:
        payload["diagnosis"] = {
            "is_healthy": result.diagnosis.is_healthy,
            "issues": result.diagnosis.issues,
            "suggested_continue_point": result.diagnosis.suggested_continue_point,
            "details": result.diagnosis.details,
        }
    return json.dumps(payload, indent=2, sort_keys=True)


def get_child_status_tool(args: Dict[str, Any]) -> str:
    from ..api import get_child_thread_statuses
    from ..db import ThreadsDB

    manager_id = (args.get("_thread_id") or args.get("manager_thread_id") or "").strip()
    if not manager_id:
        return "Error: manager_thread_id is required."

    tids_arg = args.get("child_thread_ids") or args.get("thread_ids") or args.get("child_thread_id") or args.get("thread_id")
    if isinstance(tids_arg, str):
        child_ids = [tids_arg]
    elif isinstance(tids_arg, list):
        child_ids = [str(t) for t in tids_arg if isinstance(t, (str, int)) and str(t).strip()]
    elif tids_arg is None:
        # No ids means: inspect all direct children of the manager thread.
        child_ids = None
    else:
        return "Error: child_thread_ids must be a string, a list of strings, or omitted."

    try:
        max_errors = int(args.get("max_errors", 5))
    except Exception:
        max_errors = 5

    try:
        statuses = get_child_thread_statuses(
            ThreadsDB(),
            manager_id,
            child_ids,
            max_errors=max_errors,
        )
    except Exception as e:
        return f"Error: {e}"

    payload = {"children": [st.to_dict() for st in statuses]}
    return json.dumps(payload, indent=2, sort_keys=True)


def wait_tool(args: Dict[str, Any]) -> str:
    from ..api import _clean_wait_thread_id, wait_for_threads
    from ..db import ThreadsDB

    tids_arg = args.get("thread_ids") or args.get("threads") or args.get("thread_id")
    if "thread_ids" not in args and "threads" not in args and "thread_id" not in args:
        tid_context = args.get("_thread_id")
        if isinstance(tid_context, str) and tid_context.strip():
            # Convenience for REPL code: eggtools.wait(tid) sends the id as the
            # raw positional argument, which ToolRegistry stores under _arg
            # before injecting _thread_id. Treat _arg as the target thread id.
            raw = args.get("_arg")
            if isinstance(raw, (str, int)):
                tids_arg = str(raw)
    if isinstance(tids_arg, str):
        thread_ids = [_clean_wait_thread_id(tids_arg)]
    elif isinstance(tids_arg, list):
        thread_ids = [_clean_wait_thread_id(t) for t in tids_arg if isinstance(t, (str, int))]
    else:
        return 'Error: "thread_ids" must be a string or a list of strings.'
    thread_ids = [tid for tid in thread_ids if tid]
    if not thread_ids:
        return "Error: no valid thread_ids provided."

    timeout_sec = resolve_tool_timeout_arg(args)

    db = ThreadsDB()
    results = wait_for_threads(db, thread_ids, timeout_sec=timeout_sec, poll_interval=0.2)

    lines: list[str] = []
    for tid in thread_ids:
        short = tid[-8:]
        res = results.get(tid)
        if res is not None and res.finished:
            content = res.last_assistant_message or "(no assistant content found)"
            lines.append(f"Thread {short} finished. Last assistant message:\n{content}")
        else:
            st = res.state if res is not None else "unknown"
            if st == "not_found":
                lines.append(f"Thread {short} not found; not waiting.")
            else:
                lines.append(f"Thread {short} not finished (state={st}).")
    return "\n\n".join(lines)


def _command_target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _command_log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def _command_log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _command_start_scheduler(context: Any, thread_id: str) -> None:
    if context.start_scheduler is not None:
        context.start_scheduler(thread_id)


def _command_append_message(context: Any, *args: Any, **kwargs: Any) -> Any:
    if context.append_message is not None:
        return context.append_message(*args, **kwargs)
    from ..api import append_message

    return append_message(*args, **kwargs)


def _command_create_snapshot(context: Any, *args: Any, **kwargs: Any) -> Any:
    if context.create_snapshot is not None:
        return context.create_snapshot(*args, **kwargs)
    from ..api import create_snapshot

    return create_snapshot(*args, **kwargs)


def _command_approve_tool_calls(context: Any, *args: Any, **kwargs: Any) -> Any:
    if context.approve_tool_calls is not None:
        return context.approve_tool_calls(*args, **kwargs)
    from ..api import approve_tool_calls_for_thread

    return approve_tool_calls_for_thread(*args, **kwargs)


def _command_resolve_thread_selector(context: Any, selector: str) -> str | None:
    from .thread_ui import resolve_thread_selector

    return resolve_thread_selector(context, selector)


def spawn_child_thread_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    from ..tools import create_default_tools

    target = _command_target(context, "spawnChildThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target

    try:
        child = create_default_tools().execute(
            "spawn_agent",
            {
                "parent_thread_id": current_thread,
                "context_text": arg or "Spawned task",
                "label": "spawn",
                "system_prompt": context.system_prompt or "You are a helpful assistant.",
            },
        )
    except Exception as e:
        _command_log(context, f"/spawn error: {e}")
        return CommandResult(clear_input=False)
    if not isinstance(child, str):
        _command_log(context, f"/spawn returned non-string thread id: {child!r}")
        return CommandResult(clear_input=False)

    _command_start_scheduler(context, child)
    _command_log(context, f"Spawned thread: {child[-8:]}")
    try:
        command_text = f"/spawnChildThread {arg}".strip()
        message = f"Command: {command_text}\n\nOutput:\n{child}"
        _command_append_message(context, db, current_thread, "user", message, extra={"keep_user_turn": True})
        _command_create_snapshot(context, db, current_thread)
    except Exception:
        pass
    return CommandResult(clear_input=True, start_schedulers=(child,))


def spawn_auto_child_thread_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    from ..tools import create_default_tools

    target = _command_target(context, "spawnAutoApprovedChildThread")
    if target is None:
        return CommandResult(clear_input=False)
    _, current_thread = target

    try:
        child = create_default_tools().execute(
            "spawn_agent_auto",
            {
                "parent_thread_id": current_thread,
                "context_text": arg or "Spawned task",
                "label": "spawn_auto",
                "system_prompt": context.system_prompt or "You are a helpful assistant.",
            },
        )
    except Exception as e:
        _command_log(context, f"/spawn_auto error: {e}")
        return CommandResult(clear_input=False)
    if not isinstance(child, str):
        _command_log(context, f"/spawn_auto returned non-string thread id: {child!r}")
        return CommandResult(clear_input=False)

    _command_start_scheduler(context, child)
    _command_log(context, f"Spawned auto-approval thread: {child[-8:]}")
    return CommandResult(clear_input=True, start_schedulers=(child,))


def wait_for_threads_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _command_target(context, "waitForThreads")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target

    arg_text = (arg or "").strip()
    if not arg_text:
        _command_log(context, "Usage: /wait <thread-id|suffix|name|recap-fragment>[,more...]")
        return CommandResult(clear_input=False)

    resolved: list[str] = []
    for selector in [part for part in re.split(r"[\s,]+", arg_text) if part]:
        thread_id = _command_resolve_thread_selector(context, selector)
        if not thread_id:
            _command_log(context, f"/wait: no thread matches selector '{selector}'")
            return CommandResult(clear_input=False)
        resolved.append(thread_id)

    tc_id = os.urandom(8).hex()
    tool_call = {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": "wait",
            "arguments": json.dumps({"thread_ids": resolved}, ensure_ascii=False),
        },
    }
    extra = {
        "tool_calls": [tool_call],
        "keep_user_turn": True,
        "user_command_type": "/wait",
    }
    _command_append_message(context, db, current_thread, "user", f"/wait {arg_text}", extra=extra)
    try:
        _command_approve_tool_calls(
            context,
            db,
            current_thread,
            decision="granted",
            reason="Approved as user-initiated /wait command",
            tool_call_id=tc_id,
        )
    except Exception as e:
        _command_log(context, f"Error approving tool call for wait command: {e}")
    try:
        _command_create_snapshot(context, db, current_thread)
    except Exception:
        pass
    _command_start_scheduler(context, current_thread)
    _command_log(context, f"Queued /wait for threads: {' '.join([thread_id[-8:] for thread_id in resolved])}.")
    return CommandResult(clear_input=True, start_schedulers=(current_thread,))


def register_subagent_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("spawnChildThread", spawn_child_thread_command, category="subagents", usage="/spawnChildThread <text>", description="Spawn a child thread."))
    registry.register(CommandSpec("spawnAutoApprovedChildThread", spawn_auto_child_thread_command, category="subagents", usage="/spawnAutoApprovedChildThread <text>", description="Spawn an auto-approved child thread."))
    registry.register(CommandSpec("waitForThreads", wait_for_threads_command, category="subagents", usage="/waitForThreads <threads>", description="Wait for child threads."))


def register_subagent_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="spawn_agent",
        description=(
            "Spawn a child agent as a new thread under the current task. "
            "Use this whenever you want to delegate a sub-problem to a "
            "child agent. Provide a short natural-language description of "
            "the sub-task in context_text. The child agent will run "
            "independently and eventually produce an assistant message as "
            'its result. To retrieve that result, call the "wait" tool on '
            "the returned thread id and read the last assistant message."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "system_prompt": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "disabled_tools": {"type": "array", "items": {"type": "string"}},
                "share_session": {"type": "boolean"},
                "share_repl": {"type": "boolean"},
            },
            "required": ["context_text"],
        },
        impl=spawn_agent_tool,
        local_only=False,
    )

    registry.register(
        name="spawn_agent_auto",
        description=(
            "Like spawn_agent, but configures the spawned child thread to "
            "have global tool auto-approval. The child agent can call "
            "tools without further approval events. Use context_text to "
            'describe the delegated sub-task, then use the "wait" tool on '
            "the returned thread id to read its final assistant message."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "context_text": {"type": "string"},
                "label": {"type": "string"},
                "system_prompt": {"type": "string"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "disabled_tools": {"type": "array", "items": {"type": "string"}},
                "share_session": {"type": "boolean"},
                "share_repl": {"type": "boolean"},
            },
            "required": ["context_text"],
        },
        impl=spawn_agent_auto_tool,
        local_only=False,
    )

    registry.register(
        name="send_message_to_child",
        description=(
            "Append a message to a child or descendant thread so it can continue from its existing context. "
            "If the child is currently waiting in get_user_message_while_preserving_llm_turn, this answers that tool call; "
            "otherwise it behaves like a normal child user message. "
            "Use this for manager/worker guidance loops after a child has produced an initial response. "
            "The target must be a descendant of the calling thread. This tool does not wait; call wait afterwards."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "child_thread_id": {"type": "string", "description": "Target child or descendant thread id."},
                "message": {"type": "string", "description": "Guidance/user message to append to the target thread."},
                "require_idle": {
                    "type": "boolean",
                    "description": "When true (default), refuse to message a running/runnable child.",
                },
            },
            "required": ["child_thread_id", "message"],
        },
        impl=send_message_to_child_tool,
        local_only=False,
    )

    registry.register(
        name="continue_subthread",
        description=(
            "Repair or continue a child/descendant subthread after LLM/runner failures, analogous to the user /continue command. "
            "The target must be a descendant of the calling thread and must not have an active lease."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "child_thread_id": {"type": "string", "description": "Target child or descendant thread id."},
                "msg_id": {"type": "string", "description": "Optional message id to continue from; omit for auto-diagnosis."},
            },
            "required": ["child_thread_id"],
        },
        impl=continue_subthread_tool,
        local_only=False,
    )

    registry.register(
        name="get_child_status",
        description=(
            "Inspect child or descendant thread status without waiting. Returns JSON with each child's "
            "coarse state, current provider context_tokens, full_thread_tokens, concise compaction info, "
            "optional context limit percentage, open invoke, "
            "active assistant_notes from the current unfinished assistant workflow, "
            "last event metadata, and recent LLM/runner/session/tool errors. Omit child_thread_ids to inspect "
            "all direct children of the calling thread."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "child_thread_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional child/descendant thread ids. Omit to inspect all direct children.",
                },
                "max_errors": {
                    "type": "integer",
                    "description": "Maximum recent error items to include per child (default 5, capped at 20).",
                },
            },
        },
        impl=get_child_status_tool,
        local_only=False,
    )

    registry.register(
        name="wait",
        description=(
            "Wait for one or more threads to finish and return their last "
            "assistant message. A thread is considered finished when its "
            "state is 'waiting_user'. Optional timeout_sec limits how long to wait."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "thread_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of thread_ids to wait for.",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Maximum seconds to wait before returning.",
                },
            },
            "required": ["thread_ids"],
        },
        impl=wait_tool,
        local_only=False,
    )


@dataclass(frozen=True)
class SubagentsPlugin:
    name: str = "subagents"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.tool_registry is not None:
            register_subagent_tools(context.tool_registry)
        if context.command_registry is not None:
            register_subagent_commands(context.command_registry)


__all__ = [
    "SubagentsPlugin",
    "apply_spawn_child_configuration",
    "clean_bool_arg",
    "clean_optional_text",
    "continue_subthread_tool",
    "get_child_status_tool",
    "inherited_system_prompt",
    "register_subagent_tools",
    "register_subagent_commands",
    "send_message_to_child_tool",
    "spawn_auto_child_thread_command",
    "spawn_agent_auto_tool",
    "spawn_agent_tool",
    "spawn_child_thread_command",
    "wait_for_threads_command",
    "wait_tool",
]
