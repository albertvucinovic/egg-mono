from __future__ import annotations

"""Built-in thread UI commands.

This plugin owns commands for listing, selecting, creating, duplicating,
deleting, and continuing threads. It intentionally remains UI-oriented: core
thread persistence and safety checks stay in eggthreads.api, while this module
adapts slash-command text and frontend callbacks to those core services.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from ..plugins import PluginContext


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


def _schedule_coro(coro_factory: Callable[[], Any] | None) -> None:
    if coro_factory is None:
        return
    try:
        loop = asyncio.get_running_loop()
        coro = coro_factory()
        try:
            task = loop.create_task(coro)
            if task is None and hasattr(coro, "close"):
                coro.close()
        except Exception:
            if hasattr(coro, "close"):
                coro.close()
    except Exception:
        pass


def _set_current_thread(context: Any, thread_id: str) -> None:
    if context.set_current_thread is not None:
        context.set_current_thread(thread_id)
    elif context.app is not None:
        context.app.current_thread = thread_id


def _start_scheduler(context: Any, thread_id: str) -> None:
    if context.start_scheduler is not None:
        context.start_scheduler(thread_id)


def _print_current_thread(context: Any, heading: str) -> None:
    if context.print_current_thread is not None:
        try:
            context.print_current_thread(heading=heading)
        except TypeError:
            context.print_current_thread(heading)


def _current_model_for_thread(context: Any, thread_id: str) -> str | None:
    if context.get_current_model is not None:
        return context.get_current_model(thread_id)
    if context.app is not None and hasattr(context.app, "current_model_for_thread"):
        return context.app.current_model_for_thread(thread_id)
    try:
        from ..api import current_thread_model

        return current_thread_model(context.db, thread_id)
    except Exception:
        return None


def select_threads_by_selector(context: Any, selector: str) -> list[str]:
    if context.select_threads is not None:
        return list(context.select_threads(selector))
    if context.app is not None and hasattr(context.app, "select_threads_by_selector"):
        return list(context.app.select_threads_by_selector(selector))
    try:
        from ..api import list_threads

        rows = list_threads(context.db)
    except Exception:
        rows = []
    sel_l = (selector or "").lower()
    matches: list[str] = []
    for row in rows:
        if row.thread_id == selector:
            return [row.thread_id]
    if sel_l:
        matches = [row.thread_id for row in rows if row.thread_id.lower().endswith(sel_l)]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if sel_l in row.thread_id.lower()]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if isinstance(row.name, str) and sel_l in row.name.lower()]
    if not matches and sel_l:
        matches = [row.thread_id for row in rows if isinstance(row.short_recap, str) and sel_l in row.short_recap.lower()]
    return matches


def _sort_thread_matches_newest_first(db: Any, matches: list[str]) -> list[str]:
    try:
        from ..api import list_threads

        rows = list_threads(db)
        created_at = {row.thread_id: row.created_at for row in rows}
    except Exception:
        created_at = {}
    return sorted(matches, key=lambda tid: created_at.get(tid, ""), reverse=True)


def resolve_thread_selector(context: Any, selector: str) -> str | None:
    sel = (selector or "").strip()
    if not sel:
        return None
    matches = select_threads_by_selector(context, sel)
    if not matches and " " in sel:
        matches = select_threads_by_selector(context, sel.split()[0])
    if not matches:
        try:
            from ..api import list_threads

            suffix = sel.lower()
            matches = [row.thread_id for row in list_threads(context.db) if row.thread_id.lower().endswith(suffix)]
        except Exception:
            matches = []
    if not matches:
        return None
    return _sort_thread_matches_newest_first(context.db, matches)[0]


def new_thread_command(context: Any, arg: str):
    from ..api import append_message, create_root_thread, create_snapshot
    from ..command_catalog import CommandResult

    target = _target(context, "newThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target

    try:
        new_root = create_root_thread(
            db,
            name=(arg or "").strip() or "Root",
            initial_model_key=_current_model_for_thread(context, current_thread),
        )
        append_message(db, new_root, "system", context.system_prompt or "You are a helpful assistant.")
        create_snapshot(db, new_root)
    except Exception as e:
        _log(context, f"/newThread error: {e}")
        return CommandResult(clear_input=False)

    _start_scheduler(context, new_root)
    _set_current_thread(context, new_root)
    _schedule_coro(context.watch_current_thread)
    _log(context, f"Created new root thread: {new_root[-8:]}")
    _print_current_thread(context, heading=f"Switched to thread: {new_root}")
    return CommandResult(clear_input=True, switched_thread=new_root, start_schedulers=(new_root,))


def threads_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    try:
        text = context.format_threads() if context.format_threads is not None else "No thread formatter available."
        _log(context, "Threads by subtree (see console for full).")
        if context.console_print_block is not None:
            context.console_print_block("Threads", text, border_style="blue")
        else:
            _log(context, text)
    except Exception as e:
        _log(context, f"Error listing threads: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def thread_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    target = _target(context, "thread")
    if target is None:
        return CommandResult(clear_input=False)
    _, current_thread = target
    selector = (arg or "").strip()
    if not selector:
        _log(context, f"Current thread: {current_thread}")
        return CommandResult(clear_input=True)

    new_thread = resolve_thread_selector(context, selector)
    if not new_thread:
        _log(context, f"No thread matches selector: {selector}")
        return CommandResult(clear_input=False)
    _start_scheduler(context, new_thread)
    _set_current_thread(context, new_thread)
    _schedule_coro(context.watch_current_thread)
    _log(context, f"Switched to thread: {new_thread[-8:]}")
    _print_current_thread(context, heading=f"Switched to thread: {new_thread}")
    return CommandResult(clear_input=True, switched_thread=new_thread, start_schedulers=(new_thread,))


def delete_thread_command(context: Any, arg: str):
    from ..api import delete_thread, list_threads
    from ..command_catalog import CommandResult

    target = _target(context, "deleteThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    selector = (arg or "").strip()
    if not selector:
        _log(context, "Usage: /delete <thread-id|suffix|name|recap-fragment>")
        return CommandResult(clear_input=False)

    matches = select_threads_by_selector(context, selector)
    if not matches and " " in selector:
        matches = select_threads_by_selector(context, selector.split()[0])
    if not matches:
        try:
            suffix = selector.lower()
            matches = [row.thread_id for row in list_threads(db) if row.thread_id.lower().endswith(suffix)]
        except Exception:
            matches = []
    matches = [thread_id for thread_id in matches if thread_id != current_thread]
    if not matches:
        _log(context, "No deletable thread matches selector.")
        return CommandResult(clear_input=False)

    target_thread = _sort_thread_matches_newest_first(db, matches)[0]
    try:
        delete_thread(db, target_thread)
        _log(context, f"Thread {target_thread[-8:]} deleted.")
    except Exception as e:
        _log(context, f"Error deleting thread: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def parent_thread_command(context: Any, arg: str):
    from ..api import get_parent
    from ..command_catalog import CommandResult

    target = _target(context, "parentThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        parent_id = get_parent(db, current_thread)
    except Exception:
        parent_id = None
    if not parent_id:
        _log(context, "Already at root or no parent found.")
        return CommandResult(clear_input=False)
    _set_current_thread(context, parent_id)
    _schedule_coro(context.watch_current_thread)
    _log(context, "Moved to parent thread")
    _print_current_thread(context, heading=f"Switched to thread: {parent_id}")
    return CommandResult(clear_input=True, switched_thread=parent_id)


def list_children_command(context: Any, arg: str):
    from ..api import list_children_ids
    from ..command_catalog import CommandResult

    target = _target(context, "listChildren")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    try:
        has_children = bool(list_children_ids(db, current_thread))
    except Exception:
        has_children = False
    if not has_children:
        _log(context, "No subthreads.")
        return CommandResult(clear_input=True)
    block = context.format_threads(current_thread) if context.format_threads is not None else "No thread formatter available."
    _log(context, "Subtree (see console for full):")
    if context.console_print_block is not None:
        context.console_print_block("Subtree", block, border_style="blue")
    else:
        _log(context, block)
    return CommandResult(clear_input=True)


def duplicate_thread_command(context: Any, arg: str):
    from ..api import duplicate_thread, duplicate_thread_up_to
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult

    target = _target(context, "duplicateThread")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target

    args = parse_args(arg or "")
    source_thread_id = current_thread
    name = None
    up_to_msg_id = None
    if args.named:
        name = args.named.get("name")
        up_to_msg_id = args.named.get("msg_id")
        if "thread_id" in args.named or "threadId" in args.named:
            source_thread_id = args.named.get("thread_id") or args.named.get("threadId") or source_thread_id
    if args.positional:
        if len(args.positional) == 1:
            name = args.positional[0]
        elif len(args.positional) == 2:
            name = args.positional[0]
            up_to_msg_id = args.positional[1]
        elif len(args.positional) >= 3:
            source_thread_id = args.positional[0]
            name = args.positional[1]
            up_to_msg_id = args.positional[2]

    try:
        new_thread = duplicate_thread_up_to(db, source_thread_id, up_to_msg_id, name=name) if up_to_msg_id else duplicate_thread(db, source_thread_id, name=name)
    except Exception as e:
        _log(context, f"/duplicateThread error: {e}")
        return CommandResult(clear_input=False)
    _start_scheduler(context, new_thread)
    _log(context, f"Duplicated thread to new root: {new_thread[-8:]}")
    _set_current_thread(context, new_thread)
    _schedule_coro(context.watch_current_thread)
    _print_current_thread(context, heading=f"Switched to duplicated thread: {new_thread}")
    return CommandResult(clear_input=True, switched_thread=new_thread, start_schedulers=(new_thread,))


def continue_thread_command(context: Any, arg: str):
    from ..api import append_continue_recovery_notice, continue_thread, is_thread_continuable
    from ..arg_parser import parse_args
    from ..command_catalog import CommandResult

    target = _target(context, "continue")
    if target is None:
        return CommandResult(clear_input=False)
    db, current_thread = target
    args = parse_args(arg or "")
    msg_id = args.named.get("msg_id") or args.positional_or(0)
    delay_sec = args.get_float("wait")

    if not is_thread_continuable(db, current_thread):
        _log(context, "Thread cannot be continued (may be running or waiting for input)")
        return CommandResult(clear_input=False)

    if delay_sec is not None and delay_sec > 0:
        async def delayed_continue() -> None:
            await asyncio.sleep(delay_sec)
            result = continue_thread(db, current_thread, msg_id=msg_id)
            if result.success:
                append_continue_recovery_notice(db, current_thread, result)
                _log(context, f"After {delay_sec}s delay: {result.message}")
                _print_current_thread(context, heading=f"Continued thread: {current_thread}")
            else:
                _log(context, f"/continue error: {result.message}")

        asyncio.get_running_loop().create_task(delayed_continue())
        _log(context, f"Continue scheduled in {delay_sec}s" + (f" from message {msg_id[-8:]}" if msg_id else ""))
        return CommandResult(clear_input=True)

    result = continue_thread(db, current_thread, msg_id=msg_id)
    if result.success:
        append_continue_recovery_notice(db, current_thread, result)
        _log(context, result.message)
        _print_current_thread(context, heading=f"Continued thread: {current_thread}")
        return CommandResult(clear_input=True)
    _log(context, f"/continue error: {result.message}")
    return CommandResult(clear_input=False)


def register_thread_ui_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("threads", threads_command, category="threads", usage="/threads", description="List threads."))
    registry.register(CommandSpec("thread", thread_command, category="threads", usage="/thread <selector>", description="Switch to a thread."))
    registry.register(CommandSpec("newThread", new_thread_command, category="threads", usage="/newThread <name>", description="Create a new root thread."))
    registry.register(CommandSpec("deleteThread", delete_thread_command, category="threads", usage="/deleteThread <selector>", description="Delete a thread."))
    registry.register(CommandSpec("duplicateThread", duplicate_thread_command, category="threads", usage="/duplicateThread <name> [msg_id]", description="Duplicate the current thread."))
    registry.register(CommandSpec("parentThread", parent_thread_command, category="threads", usage="/parentThread", description="Switch to the parent thread."))
    registry.register(CommandSpec("listChildren", list_children_command, category="threads", usage="/listChildren", description="List child threads."))
    registry.register(CommandSpec("continue", continue_thread_command, category="threads", usage="/continue [msg_id=<id>]", description="Continue a thread from a specific point."))


@dataclass(frozen=True)
class ThreadUiPlugin:
    name: str = "thread_ui"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_thread_ui_commands(context.command_registry)


__all__ = [
    "ThreadUiPlugin",
    "continue_thread_command",
    "delete_thread_command",
    "duplicate_thread_command",
    "list_children_command",
    "new_thread_command",
    "parent_thread_command",
    "register_thread_ui_commands",
    "resolve_thread_selector",
    "select_threads_by_selector",
    "thread_command",
    "threads_command",
]
