from __future__ import annotations

"""Built-in authentication commands."""

from dataclasses import dataclass
from typing import Any, List

from ..plugins import PluginContext


def _log(context: Any, message: str) -> None:
    if context.log_system is not None:
        context.log_system(message)


def login_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    try:
        from eggllm.auth import TokenStore, login_browser

        store = TokenStore()
        if store.is_logged_in():
            _log(context, "Already logged in. Use /logout first to re-authenticate.")
            return CommandResult(clear_input=True)
        _log(context, "Opening browser for ChatGPT login...")
        store = login_browser(store)
        status = store.get_status()
        _log(context, f"Login successful (expires_at: {status.get('expires_at')})")
    except TimeoutError:
        _log(context, "Login timed out — no browser callback received.")
        return CommandResult(clear_input=False)
    except Exception as exc:
        _log(context, f"Login failed: {exc}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def logout_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    try:
        from eggllm.auth import logout

        logout()
        _log(context, "Logged out from ChatGPT.")
    except Exception as exc:
        _log(context, f"Logout failed: {exc}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def auth_status_command(context: Any, arg: str):
    from ..command_catalog import CommandResult

    try:
        from eggllm.auth import TokenStore

        store = TokenStore()
        status = store.get_status()
        if status["logged_in"]:
            lines: List[str] = []
            lines.append("ChatGPT OAuth Status:")
            lines.append("  logged_in:   True")
            lines.append(f"  auth_mode:   {status.get('auth_mode', 'chatgpt')}")
            lines.append(f"  expires_at:  {status.get('expires_at')}")
            block = "\n".join(lines)
            _log(context, "Auth status (see console for details).")
            if context.console_print_block is not None:
                context.console_print_block("Auth Status", block, border_style="green")
            else:
                _log(context, block)
        else:
            _log(context, "Not logged in. Use /login to authenticate with ChatGPT.")
    except Exception as exc:
        _log(context, f"Auth status check failed: {exc}")
        return CommandResult(clear_input=False)
    return CommandResult(clear_input=True)


def register_auth_commands(registry: Any) -> None:
    from ..command_catalog import CommandSpec

    registry.register(CommandSpec("login", login_command, category="auth", usage="/login", description="Start ChatGPT OAuth login."))
    registry.register(CommandSpec("logout", logout_command, category="auth", usage="/logout", description="Clear ChatGPT OAuth tokens."))
    registry.register(CommandSpec("authStatus", auth_status_command, category="auth", usage="/authStatus", description="Show ChatGPT OAuth status."))


@dataclass(frozen=True)
class AuthPlugin:
    name: str = "auth"
    version: str = "0"

    def register(self, context: PluginContext) -> None:
        if context.command_registry is not None:
            register_auth_commands(context.command_registry)


__all__ = ["AuthPlugin", "auth_status_command", "login_command", "logout_command", "register_auth_commands"]
