"""Shared HTTP helpers for provider requests."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any, Dict

from .auth import TokenStore


def build_provider_headers(
    provider_name: str,
    provider_config: Dict[str, Any],
    *,
    accept_sse: bool = False,
) -> Dict[str, str]:
    """Build request headers for a provider.

    This centralizes authentication/header logic shared by streaming calls and
    auxiliary requests such as catalog refreshes.
    """

    headers = {"Content-Type": "application/json"}
    auth_type = provider_config.get("auth_type", "api_key")

    if auth_type == "chatgpt_oauth":
        store = TokenStore()
        if not store.is_logged_in():
            raise EnvironmentError("Not logged in to ChatGPT. Run /login first.")

        token = store.get_access_token()  # auto-refreshes
        headers["Authorization"] = f"Bearer {token}"

        account_id = store.get_account_id()
        if account_id:
            headers["chatgpt-account-id"] = account_id
        else:
            print(
                "[eggllm] Warning: chatgpt_account_id not found in access token. "
                "Try /logout and /login again.",
                file=sys.stderr,
            )

        headers["OpenAI-Beta"] = "responses=experimental"
        headers["originator"] = "codex_cli_rs"
        headers["User-Agent"] = (
            f"eggllm/1.0 ({platform.system()} {platform.release()}; {platform.machine()})"
        )
        if accept_sse:
            headers["accept"] = "text/event-stream"
    else:
        api_key_env = provider_config.get("api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise EnvironmentError(
                    f"Env var '{api_key_env}' is not set for '{provider_name}'"
                )
            headers["Authorization"] = f"Bearer {api_key}"

    return headers