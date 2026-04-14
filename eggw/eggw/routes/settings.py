"""Thread settings API routes for eggw backend."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from eggthreads import (
    current_thread_model,
    approve_tool_calls_for_thread,
    get_thread_auto_approval_status,
    get_thread_sandbox_status,
    get_thread_sandbox_config,
    set_thread_sandbox_config,
    is_user_sandbox_control_enabled,
)

from .. import core
router = APIRouter(prefix="/api/threads", tags=["settings"])


@router.get("/{thread_id}/settings")
async def get_thread_settings(thread_id: str):
    """Get thread settings including auto-approval status."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    return {
        "auto_approval": get_thread_auto_approval_status(core.db, thread_id),
        "model_key": current_thread_model(core.db, thread_id),
    }


@router.get("/{thread_id}/sandbox")
async def get_thread_sandbox(thread_id: str):
    """Get sandbox status for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        status = get_thread_sandbox_status(core.db, thread_id)
        user_control = True
        try:
            user_control = is_user_sandbox_control_enabled(core.db, thread_id)
        except Exception:
            pass

        return {
            "enabled": status.get("enabled", False),
            "effective": status.get("effective", False),
            "available": status.get("available", False),
            "provider": status.get("provider"),
            "config_source": status.get("config_source"),
            "config_path": status.get("config_path"),
            "warning": status.get("warning"),
            "user_control_enabled": user_control,
        }
    except Exception as e:
        return {
            "enabled": False,
            "effective": False,
            "available": False,
            "error": str(e),
        }


@router.post("/{thread_id}/sandbox")
async def set_thread_sandbox(thread_id: str, enabled: bool = True, config_name: Optional[str] = None):
    """Set sandbox configuration for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        if not is_user_sandbox_control_enabled(core.db, thread_id):
            raise HTTPException(status_code=403, detail="User sandbox control is disabled for this thread")
    except HTTPException:
        raise
    except Exception:
        pass  # Older version, assume enabled

    try:
        if config_name:
            set_thread_sandbox_config(
                core.db,
                thread_id,
                enabled=enabled,
                config_name=config_name,
                reason='API',
            )
        else:
            cfg = get_thread_sandbox_config(core.db, thread_id)
            set_thread_sandbox_config(
                core.db,
                thread_id,
                enabled=enabled,
                settings=cfg.settings,
                reason='API',
            )

        status = get_thread_sandbox_status(core.db, thread_id)
        return {
            "enabled": status.get("enabled", False),
            "effective": status.get("effective", False),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{thread_id}/settings/auto-approval")
async def set_auto_approval(thread_id: str, enabled: bool = True):
    """Enable or disable auto-approval for a thread."""
    if not core.db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    t = core.db.get_thread(thread_id)
    if not t:
        raise HTTPException(status_code=404, detail="Thread not found")

    current_state = get_thread_auto_approval_status(core.db, thread_id)

    # Only emit event if state is changing
    if current_state != enabled:
        decision = "global_approval" if enabled else "revoke_global_approval"
        reason = f"Auto-approval {'enabled' if enabled else 'disabled'} via API"
        approve_tool_calls_for_thread(core.db, thread_id, decision=decision, reason=reason)

    return {"auto_approval": enabled}
