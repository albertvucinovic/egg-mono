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

    set_thread_model(core.db, thread_id, request.model_key)
    return {"status": "ok", "model_key": request.model_key}
