"""OAuth token persistence and automatic refresh for OpenAI ChatGPT subscriptions."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import requests

# OpenAI OAuth constants
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Default token storage path
DEFAULT_AUTH_PATH = Path.home() / ".eggllm" / "auth.json"

# Refresh tokens 5 minutes before they expire
REFRESH_MARGIN_SECONDS = 300


def _extract_account_id_from_jwt(id_token: str) -> Optional[str]:
    """Parse the id_token JWT and extract chatgpt_account_id from the
    'https://api.openai.com/auth' claim."""
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        # JWT payload is base64url-encoded; add padding as needed
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        claims = json.loads(payload_bytes)
        auth_claim = claims.get("https://api.openai.com/auth", {})
        return auth_claim.get("chatgpt_account_id")
    except Exception:
        return None


class TokenStore:
    """Manages OAuth token storage, retrieval, and automatic refresh.

    Tokens are persisted to ~/.eggllm/auth.json with the structure:
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "...",
                "refresh_token": "...",
                "id_token": "...",
                "expires_at": 1234567890
            },
            "chatgpt_account_id": "...",
            "last_refresh": 1234567890
        }
    """

    def __init__(self, auth_path: Optional[Path] = None):
        self.auth_path = auth_path or DEFAULT_AUTH_PATH
        self._data: Optional[Dict[str, Any]] = None

    def _load(self) -> Dict[str, Any]:
        """Load token data from disk."""
        if self._data is not None:
            return self._data
        if self.auth_path.exists():
            try:
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        return self._data

    def _save(self, data: Dict[str, Any]) -> None:
        """Write token data to disk, creating parent directories as needed."""
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._data = data

    def is_logged_in(self) -> bool:
        """Check whether we have stored tokens (access + refresh)."""
        data = self._load()
        tokens = data.get("tokens", {})
        return bool(tokens.get("access_token") and tokens.get("refresh_token"))

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing automatically if needed.

        Raises EnvironmentError if not logged in or refresh fails.
        """
        if not self.is_logged_in():
            raise EnvironmentError("Not logged in to ChatGPT. Run /login first.")
        self.refresh_if_needed()
        data = self._load()
        return data["tokens"]["access_token"]

    def get_account_id(self) -> Optional[str]:
        """Return the chatgpt_account_id extracted from the id_token JWT."""
        data = self._load()
        return data.get("chatgpt_account_id")

    def store_tokens(
        self,
        access_token: str,
        refresh_token: str,
        id_token: Optional[str] = None,
        expires_at: Optional[int] = None,
    ) -> None:
        """Persist a new set of tokens to disk."""
        account_id = _extract_account_id_from_jwt(id_token) if id_token else None
        data = {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token or "",
                "expires_at": expires_at or 0,
            },
            "chatgpt_account_id": account_id,
            "last_refresh": int(time.time()),
        }
        self._save(data)

    def refresh_if_needed(self) -> None:
        """Refresh the access token if it expires within REFRESH_MARGIN_SECONDS."""
        data = self._load()
        tokens = data.get("tokens", {})
        expires_at = tokens.get("expires_at", 0)

        if expires_at and (time.time() + REFRESH_MARGIN_SECONDS) < expires_at:
            return  # Still valid

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise EnvironmentError("No refresh token available. Run /login again.")

        try:
            resp = requests.post(
                TOKEN_URL,
                data=urlencode({
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                }),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise EnvironmentError(f"Token refresh failed: {exc}") from exc

        new_access = body.get("access_token")
        new_refresh = body.get("refresh_token", refresh_token)
        new_id = body.get("id_token", tokens.get("id_token", ""))
        expires_in = body.get("expires_in", 3600)
        new_expires_at = int(time.time()) + int(expires_in)

        self.store_tokens(new_access, new_refresh, new_id, new_expires_at)

    def get_status(self) -> Dict[str, Any]:
        """Return a summary of the current auth state."""
        data = self._load()
        tokens = data.get("tokens", {})
        logged_in = self.is_logged_in()
        return {
            "logged_in": logged_in,
            "expires_at": tokens.get("expires_at") if logged_in else None,
            "auth_mode": data.get("auth_mode") if logged_in else None,
        }

    def clear(self) -> None:
        """Delete stored tokens (logout)."""
        if self.auth_path.exists():
            self.auth_path.unlink()
        self._data = None
