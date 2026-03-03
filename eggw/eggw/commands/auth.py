"""Authentication commands for eggw backend (/login, /logout, /authStatus)."""
from __future__ import annotations

from ..models import CommandResponse
from .. import core


async def cmd_login(thread_id: str) -> CommandResponse:
    """Trigger the OAuth PKCE browser login flow for ChatGPT."""
    try:
        from eggllm.auth import login_browser
        store = login_browser()
        return CommandResponse(
            success=True,
            message="Successfully logged in to ChatGPT.",
            data=store.get_status(),
        )
    except TimeoutError:
        return CommandResponse(success=False, message="Login timed out — no browser callback received.")
    except Exception as exc:
        return CommandResponse(success=False, message=f"Login failed: {exc}")


async def cmd_logout(thread_id: str) -> CommandResponse:
    """Clear stored OAuth tokens."""
    try:
        from eggllm.auth import logout
        logout()
        return CommandResponse(success=True, message="Logged out from ChatGPT.")
    except Exception as exc:
        return CommandResponse(success=False, message=f"Logout failed: {exc}")


async def cmd_auth_status(thread_id: str) -> CommandResponse:
    """Return the current ChatGPT OAuth status."""
    try:
        from eggllm.auth import TokenStore
        store = TokenStore()
        status = store.get_status()
        if status["logged_in"]:
            msg = f"Logged in (expires_at: {status['expires_at']})"
        else:
            msg = "Not logged in. Use /login to authenticate with ChatGPT."
        return CommandResponse(success=True, message=msg, data=status)
    except Exception as exc:
        return CommandResponse(success=False, message=f"Auth status check failed: {exc}")
