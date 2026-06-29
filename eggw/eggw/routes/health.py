"""Health check route for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter

from .. import core
from ..core.scheduler import scheduler_running

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Health check endpoint."""
    live_roots = [root_id for root_id in core.active_schedulers if scheduler_running(root_id)]
    return {
        "status": "ok",
        "db_connected": core.db is not None,
        "db_initialized": core.db is not None,
        "schedulers_active": len(live_roots),
        "scheduler_roots": live_roots,
        "models_loaded": len(core.models_config) > 0,
    }
