"""Health check route for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter

from .. import core

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "db_connected": core.db is not None,
        "db_initialized": core.db is not None,
        "schedulers_active": len(core.active_schedulers),
        "models_loaded": len(core.models_config) > 0,
    }
