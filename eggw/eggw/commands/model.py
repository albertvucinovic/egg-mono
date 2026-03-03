"""Model management commands for eggw backend."""
from __future__ import annotations

from eggthreads import current_thread_model, set_thread_model

from ..models import CommandResponse
from .. import core
from ..core import ALL_MODELS_PATH


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
    if model_name not in core.models_config:
        # Try partial match
        matches = [k for k in core.models_config.keys() if model_name.lower() in k.lower()]
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

    set_thread_model(core.db, thread_id, model_name)
    return CommandResponse(
        success=True,
        message=f"Model changed to: {model_name}",
        data={"model_key": model_name},
    )


async def cmd_update_all_models(provider: str) -> CommandResponse:
    """Handle /updateAllModels command - refresh model catalog for a provider."""
    provider = provider.strip()
    if not provider:
        return CommandResponse(
            success=True,
            message="Usage: /updateAllModels <provider>\nAvailable providers: openai, anthropic, google, etc.",
        )

    try:
        from eggllm import AllModelsCatalog
        catalog = AllModelsCatalog(str(ALL_MODELS_PATH))
        result = catalog.update_provider(provider)

        if result.get("success"):
            count = result.get("models_count", 0)
            return CommandResponse(
                success=True,
                message=f"Updated {provider} catalog: {count} models",
                data={"provider": provider, "models_count": count},
            )
        else:
            error = result.get("error", "Unknown error")
            return CommandResponse(success=False, message=f"Failed to update {provider}: {error}")
    except Exception as e:
        return CommandResponse(success=False, message=f"/updateAllModels error: {e}")
