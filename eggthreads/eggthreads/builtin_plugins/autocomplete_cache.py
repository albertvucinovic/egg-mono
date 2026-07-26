from __future__ import annotations

"""User-facing operations for the disposable full-history autocomplete sidecar."""

from dataclasses import dataclass
from typing import Any

from ..plugins import PluginContext


def _target(context: Any) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    return (db, str(thread_id)) if db is not None and thread_id else None


def autocomplete_cache_command(context: Any, arg: str):
    from ..autocomplete_sidecar import (
        autocomplete_catalog_status,
        catch_up_autocomplete_catalog,
        build_autocomplete_catalog,
        clear_autocomplete_catalog,
    )
    from ..command_catalog import CommandResult

    target = _target(context)
    if target is None:
        return CommandResult(clear_input=False, message="/autocompleteCache failed: no current thread.")
    db, thread_id = target
    action = str(arg or "status").strip().casefold() or "status"
    if action in {"warm", "rebuild"}:
        manager = context.autocomplete_sidecar_manager
        if manager is None:
            manager = getattr(context.app, "_autocomplete_sidecar_manager", None)
        if manager is None:
            manager = getattr(getattr(context.app, "state", None), "autocomplete_sidecar_manager", None)
        if manager is not None and action == "warm":
            manager.request_build(thread_id)
            return CommandResult(clear_input=True, message="Autocomplete cache build scheduled.")
        result = (
            build_autocomplete_catalog(db, thread_id)
            if action == "rebuild"
            else catch_up_autocomplete_catalog(db, thread_id)
        )
        return CommandResult(
            clear_input=result.state == "ready",
            message=(
                f"Autocomplete cache ready: {result.total} records through {result.through_event_seq}."
                if result.state == "ready"
                else f"Autocomplete cache {result.state}: {result.error or 'another builder owns it'}."
            ),
        )
    if action == "clear":
        removed = clear_autocomplete_catalog(db, thread_id)
        return CommandResult(clear_input=True, message="Autocomplete cache cleared." if removed else "Autocomplete cache was already absent.")
    if action not in {"status", "verify"}:
        return CommandResult(clear_input=False, message="Usage: /autocompleteCache [status|verify|warm|rebuild|clear]")
    status = autocomplete_catalog_status(db, thread_id)
    path = str(status.sidecar_path) if status.sidecar_path is not None else "unknown"
    return CommandResult(
        clear_input=True,
        message=(
            f"Autocomplete cache: {status.state}\n"
            f"  path: {path}\n"
            f"  version: 4\n"
            f"  source watermark: {status.through_event_seq}\n"
            f"  build target: {status.target_event_seq}\n"
            f"  size: {status.size_bytes} bytes\n"
            f"  owner: {status.owner or '-'}\n"
            f"  last error: {status.last_error or '-'}"
        ),
    )


def autocomplete_cache_completions(context: Any, arg: str):
    fragment = str(arg or "").strip().casefold()
    return [item for item in ("status", "verify", "warm", "rebuild", "clear") if not fragment or item.startswith(fragment)]


def register_autocomplete_cache_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec(
        "autocompleteCache",
        autocomplete_cache_command,
        category="diagnostics",
        usage="/autocompleteCache [status|verify|warm|rebuild|clear]",
        description="Inspect or maintain the disposable full-history autocomplete sidecar.",
        complete=autocomplete_cache_completions,
    ))


@dataclass(frozen=True)
class AutocompleteCachePlugin:
    name: str = "autocomplete-cache"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_autocomplete_cache_commands(context.command_registry)
