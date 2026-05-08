from __future__ import annotations

"""Built-in model selection/catalog commands."""

import os
from dataclasses import dataclass
from typing import Any, Dict, List

from eggllm.catalog import format_update_all_models_text

from ..plugins import PluginContext


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def _print_block(context: Any, title: str, text: str, *, border_style: str = "blue") -> None:
    if context.console_print_block is not None:
        context.console_print_block(title, text, border_style=border_style)
    else:
        _log(context, text)


def _target(context: Any, command_name: str) -> tuple[Any, str] | None:
    db = context.db if context.db is not None else getattr(context.app, "db", None)
    thread_id = context.current_thread or getattr(context.app, "current_thread", None)
    if db is None or not thread_id:
        _log(context, f"/{command_name} failed: no current thread.")
        return None
    return db, thread_id


def _models_path() -> str:
    try:
        from egg.utils import MODELS_PATH  # type: ignore

        return str(MODELS_PATH)
    except Exception:
        return "models.json"


def _all_models_path() -> str:
    try:
        from egg.utils import ALL_MODELS_PATH  # type: ignore

        return str(ALL_MODELS_PATH)
    except Exception:
        return "all-models.json"


def format_model_info(concrete_model_info: Any, model_key: str | None = None) -> str:
    import json

    if not concrete_model_info or concrete_model_info == {}:
        return f"Model: {model_key}\nNo concrete configuration available." if model_key else "No concrete configuration available."
    result = json.dumps(concrete_model_info, indent=2)
    return f"Model: {model_key}\n{result}" if model_key else result


def _current_model(context: Any, thread_id: str) -> str | None:
    if context.get_current_model is not None:
        return context.get_current_model(thread_id)
    app = getattr(context, "app", None)
    if app is not None and hasattr(app, "current_model_for_thread"):
        return app.current_model_for_thread(thread_id)
    try:
        from ..api import current_thread_model

        db = context.db if context.db is not None else getattr(app, "db", None)
        return current_thread_model(db, thread_id) if db is not None else None
    except Exception:
        return None


def _format_for_context(context: Any, concrete: Any, model_key: str | None = None) -> str:
    app = getattr(context, "app", None)
    if app is not None and hasattr(app, "format_model_info"):
        return app.format_model_info(concrete, model_key)
    return format_model_info(concrete, model_key)


def model_command(context: Any, arg: str):
    from ..command_catalog import CommandResult
    import eggthreads as _eggthreads

    target = _target(context, "model")
    if target is None:
        return CommandResult(clear_input=False)
    db, thread_id = target
    arg2 = (arg or "").strip()
    llm = context.llm_client if context.llm_client is not None else getattr(context.app, "llm_client", None)
    if arg2:
        try:
            _eggthreads.set_thread_model(db, thread_id, arg2, reason="ui /model", models_path=_models_path())
            db.append_event(
                event_id=os.urandom(10).hex(),
                thread_id=thread_id,
                type_="msg.create",
                msg_id=os.urandom(10).hex(),
                payload={"role": "user", "content": f"/model {arg2}", "no_api": True},
            )
            _eggthreads.create_snapshot(db, thread_id)
            concrete = _eggthreads.current_thread_model_info(db, thread_id)
            formatted = _format_for_context(context, concrete, arg2)
            _log(context, "Model set (see console for full).")
            _print_block(context, "Model", formatted, border_style="blue")
        except Exception as e:
            _log(context, f"/model error: {e}")
            return CommandResult(clear_input=False)
        return CommandResult(clear_input=True)

    cur_model = _current_model(context, thread_id)
    if cur_model:
        concrete = _eggthreads.current_thread_model_info(db, thread_id)
        formatted = _format_for_context(context, concrete, cur_model)
        _log(context, "Current model configuration (see console).")
        _print_block(context, "Model", formatted, border_style="blue")
    else:
        _log(context, "No model selected for this thread.")

    try:
        if not llm:
            _log(context, "Models not available (llm client not initialized).")
        else:
            by_provider: Dict[str, List[str]] = {}
            for name, cfg in (llm.registry.models_config or {}).items():
                prov = cfg.get("provider", "unknown")
                by_provider.setdefault(prov, []).append(name)
            lines: List[str] = []
            for prov in sorted(by_provider.keys()):
                lines.append(f"{prov}:")
                for model_name in sorted(by_provider[prov]):
                    lines.append(f"  - {model_name}")
            lines.append("\nTip: use 'all:provider:model' to pick catalog models.")
            _log(context, "Available models:\n" + "\n".join(lines))
    except Exception as e:
        _log(context, f"Error listing models: {e}")
    return CommandResult(clear_input=True)


def update_all_models_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    provider = (arg or "").strip()
    llm = context.llm_client if context.llm_client is not None else getattr(context.app, "llm_client", None)
    try:
        if not provider:
            providers_config = {}
            try:
                registry = getattr(llm, "registry", None)
                providers_config = getattr(registry, "providers_config", {}) or {}
            except Exception:
                providers_config = {}
            block = format_update_all_models_text(providers_config, all_models_path=_all_models_path())
            _log(context, "Usage: /updateAllModels <provider> (see console for full details).")
            _print_block(context, "Update All Models", block, border_style="blue")
            return CommandResult(clear_input=False)
        if llm is None:
            _log(context, "Update-all-models not available (llm client not initialized).")
            return CommandResult(clear_input=False)
        res = llm.update_all_models(provider)
        result_text = res if isinstance(res, str) else str(res)
        block = format_update_all_models_text(
            llm.registry.providers_config,
            provider=provider,
            result=result_text,
            all_models_path=_all_models_path(),
        )
        status = "ok"
        if result_text.startswith("Error:"):
            status = "error"
        elif result_text.startswith("Warning:"):
            status = "warning"
        border = "green" if status == "ok" else ("yellow" if status == "warning" else "red")
        summary = "Update-all-models result (see console for details)." if status == "ok" else "Update-all-models issue (see console for details)."
        _log(context, summary)
        _print_block(context, "Update All Models", block, border_style=border)
    except Exception as e:
        _log(context, f"Update All Models error: {e}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def register_model_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("model", model_command, category="model", usage="/model <key>", description="Set or display the active model."))
    registry.register(CommandSpec("updateAllModels", update_all_models_command, category="model", usage="/updateAllModels <provider>", description="Refresh a provider model catalog."))


@dataclass(frozen=True)
class ModelPlugin:
    name: str = "model"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_model_commands(context.command_registry)


__all__ = ["ModelPlugin", "format_model_info", "model_command", "register_model_commands", "update_all_models_command"]
