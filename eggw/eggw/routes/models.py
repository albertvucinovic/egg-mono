"""Model API routes for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from eggthreads import set_thread_model

from ..models import ModelInfo, ModelsResponse, SetModelRequest
from .. import core

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models", response_model=ModelsResponse)
async def get_models():
    """Get available models with default."""
    models = []
    for key, config in core.models_config.items():
        if not core.is_chat_model_key(key, config, core.llm_client):
            continue
        models.append(ModelInfo(
            key=key,
            provider=config.get("provider", "unknown"),
            model_id=config.get("model_name", key),
            display_name=key,  # The key is the display name in eggllm format
        ))
    return ModelsResponse(models=models, default_model=core.default_model_key)


@router.post("/threads/{thread_id}/model")
async def set_model(thread_id: str, request: SetModelRequest):
    """Set the model for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if request.model_key not in core.models_config:
        raise HTTPException(status_code=400, detail="Invalid model key")
    if not core.is_chat_model_key(request.model_key, core.models_config.get(request.model_key) or {}, core.llm_client):
        raise HTTPException(status_code=400, detail="Model is not usable for normal chat")

    set_thread_model(
        core.db,
        thread_id,
        request.model_key,
        models_path=str(core.MODELS_PATH),
        all_models_path=str(core.ALL_MODELS_PATH),
    )
    return {"status": "ok", "model_key": request.model_key}
