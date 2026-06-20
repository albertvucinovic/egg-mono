"""Model management commands for eggw backend."""
from __future__ import annotations

from eggllm.catalog import format_update_all_models_text
from eggthreads import current_thread_model, set_thread_model

from ..models import CommandResponse
from .. import core


async def cmd_model(thread_id: str, model_name: str) -> CommandResponse:
    """Handle /model command."""
    if not model_name:
        # Return current model
        current = current_thread_model(core.db, thread_id)
        return CommandResponse(
            success=True,
            message=f"Current model: {current}",
            data={"model_key": current},
        )

    # Check if model exists
    chat_models = {k: core.models_config[k] for k in core.chat_model_keys(core.models_config, core.llm_client)}

    if model_name not in chat_models:
        # Try partial match
        matches = [k for k in chat_models.keys() if model_name.lower() in k.lower()]
        if len(matches) == 1:
            model_name = matches[0]
        elif len(matches) > 1:
            return CommandResponse(
                success=False,
                message=f"Ambiguous model name. Matches: {', '.join(matches[:5])}",
            )
        else:
            return CommandResponse(
                success=False,
                message=f"Unknown model: {model_name}",
            )
    set_thread_model(
        core.db,
        thread_id,
        model_name,
        models_path=str(core.MODELS_PATH),
        all_models_path=str(core.ALL_MODELS_PATH),
    )
    return CommandResponse(
        success=True,
        message=f"Model changed to: {model_name}",
        data={"model_key": model_name},
    )


async def cmd_update_all_models(provider: str) -> CommandResponse:
    """Handle /updateAllModels command - refresh model catalog for a provider."""
    provider = provider.strip()
    try:
        if core.llm_client is not None:
            llm = core.llm_client
        else:
            from eggllm import LLMClient

            llm = LLMClient(models_path=core.MODELS_PATH, all_models_path=core.ALL_MODELS_PATH)

        if not provider:
            return CommandResponse(
                success=True,
                message=format_update_all_models_text(
                    llm.registry.providers_config,
                    all_models_path=core.ALL_MODELS_PATH,
                ),
            )

        # Prefer the long-lived client so the in-memory catalog is updated and
        # autocomplete sees new all:provider:model entries immediately.
        result = llm.update_all_models(provider)

        ok = isinstance(result, str) and not result.startswith(("Error:", "Warning:"))
        return CommandResponse(
            success=ok,
            message=format_update_all_models_text(
                llm.registry.providers_config,
                provider=provider,
                result=result if isinstance(result, str) else str(result),
                all_models_path=core.ALL_MODELS_PATH,
            ),
            data={"provider": provider} if ok else None,
        )
    except Exception as e:
        return CommandResponse(success=False, message=f"/updateAllModels error: {e}")
