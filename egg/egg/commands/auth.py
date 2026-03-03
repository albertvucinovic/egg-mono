"""Authentication command mixins for the egg CLI application."""
from __future__ import annotations

from typing import List


class AuthCommandsMixin:
    """Mixin providing auth commands: /login, /logout, /authStatus."""

    def cmd_login(self, arg: str) -> None:
        """Handle /login command - trigger OAuth PKCE browser login for ChatGPT."""
        try:
            from eggllm.auth import TokenStore, login_browser
            store = TokenStore()
            if store.is_logged_in():
                self.log_system("Already logged in. Use /logout first to re-authenticate.")
                return
            self.log_system("Opening browser for ChatGPT login...")
            store = login_browser(store)
            status = store.get_status()
            self.log_system(f"Login successful (expires_at: {status.get('expires_at')})")
        except TimeoutError:
            self.log_system("Login timed out — no browser callback received.")
        except Exception as exc:
            self.log_system(f"Login failed: {exc}")

    def cmd_logout(self, arg: str) -> None:
        """Handle /logout command - clear stored ChatGPT OAuth tokens."""
        try:
            from eggllm.auth import logout
            logout()
            self.log_system("Logged out from ChatGPT.")
        except Exception as exc:
            self.log_system(f"Logout failed: {exc}")

    def cmd_authStatus(self, arg: str) -> None:
        """Handle /authStatus command - show ChatGPT OAuth status."""
        try:
            from eggllm.auth import TokenStore
            store = TokenStore()
            status = store.get_status()
            if status["logged_in"]:
                lines: List[str] = []
                lines.append("ChatGPT OAuth Status:")
                lines.append(f"  logged_in:   True")
                lines.append(f"  auth_mode:   {status.get('auth_mode', 'chatgpt')}")
                lines.append(f"  expires_at:  {status.get('expires_at')}")
                block = "\n".join(lines)
                self.log_system("Auth status (see console for details).")
                self.console_print_block("Auth Status", block, border_style="green")
            else:
                self.log_system("Not logged in. Use /login to authenticate with ChatGPT.")
        except Exception as exc:
            self.log_system(f"Auth status check failed: {exc}")
