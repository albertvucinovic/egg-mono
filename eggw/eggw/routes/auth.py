"""Authentication API routes for eggw backend."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
async def auth_status():
    """Return the current ChatGPT OAuth login status."""
    from eggllm.auth import TokenStore
    store = TokenStore()
    return store.get_status()


@router.post("/login")
async def auth_login():
    """Trigger the OAuth PKCE browser login flow."""
    try:
        from eggllm.auth import login_browser
        store = login_browser()
        return {"success": True, "message": "Login successful.", **store.get_status()}
    except TimeoutError:
        return {"success": False, "message": "Login timed out — no browser callback received."}
    except Exception as exc:
        return {"success": False, "message": f"Login failed: {exc}"}


@router.post("/logout")
async def auth_logout():
    """Clear stored OAuth tokens."""
    from eggllm.auth import logout
    logout()
    return {"success": True, "message": "Logged out."}
