"""OAuth 2.0 PKCE browser login flow for OpenAI ChatGPT subscriptions."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from .token_store import TokenStore, CLIENT_ID, _obtain_api_key

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
PLATFORM_URL = "https://platform.openai.com"
SCOPES = "openid profile email offline_access"
CALLBACK_TIMEOUT = 120  # seconds to wait for the browser callback


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback with the authorization code."""

    auth_code: Optional[str] = None
    error: Optional[str] = None
    received_state: Optional[str] = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return

        if "error" in params:
            _CallbackHandler.error = params["error"][0]
            self._respond("Login failed. You can close this tab.")
            return

        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.received_state = params.get("state", [None])[0]
        self._respond("Login successful! You can close this tab.")

    def _respond(self, body: str) -> None:
        html = f"<html><body><h2>{body}</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Silence request logging
        pass


def _parse_jwt_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _check_platform_onboarding(id_token: str) -> Optional[str]:
    """Check if the user needs to complete OpenAI platform onboarding.

    Returns the setup URL if onboarding is needed, None otherwise.
    """
    claims = _parse_jwt_claims(id_token)
    auth = claims.get("https://api.openai.com/auth", {})

    # If organization_id already exists as a flat claim, no setup needed
    if auth.get("organization_id"):
        return None

    completed = auth.get("completed_platform_onboarding", False)
    if completed:
        return None

    # Extract org info for the setup redirect
    orgs = auth.get("organizations", [])
    org_id = orgs[0]["id"] if orgs else ""
    project_id = auth.get("project_id", "")
    plan_type = auth.get("chatgpt_plan_type", "")

    setup_url = (
        f"{PLATFORM_URL}/org-setup?"
        + urlencode({"p": plan_type, "t": id_token, "with_org": org_id, "project_id": project_id})
    )
    return setup_url


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge."""
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = (
        verifier_bytes.hex()
    )  # 64-char hex string — well within 43-128 range
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    # base64url-encode without padding
    code_challenge = (
        base64.urlsafe_b64encode(digest)
        .rstrip(b"=")
        .decode("ascii")
    )
    return code_verifier, code_challenge


def login_browser(store: Optional[TokenStore] = None) -> TokenStore:
    """Run the full OAuth PKCE login flow via the user's browser.

    1. Start a localhost HTTP server on port 1455.
    2. Open the OpenAI authorization URL in the default browser.
    3. Wait for the redirect callback with the authorization code.
    4. Exchange the code for tokens (Stage 1).
    5. Exchange the id_token for an API key (Stage 2 — RFC 8693).
    6. Persist tokens via TokenStore.

    Returns the TokenStore instance with fresh tokens.
    """
    if store is None:
        store = TokenStore()

    # Reset class-level state from any prior run
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None
    _CallbackHandler.received_state = None

    # 1. Spin up a localhost callback server on port 1455 (registered with
    #    OpenAI's OAuth client app_EMoamEEZ73f0CkXaXp7hrann)
    server = HTTPServer(("127.0.0.1", 1455), _CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://localhost:{port}/auth/callback"

    # 2. PKCE + state
    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    auth_params = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    })
    auth_full_url = f"{AUTH_URL}?{auth_params}"

    # 3. Open browser
    webbrowser.open(auth_full_url)

    # 4. Wait for callback (with timeout)
    server.timeout = CALLBACK_TIMEOUT
    deadline = time.monotonic() + CALLBACK_TIMEOUT
    while _CallbackHandler.auth_code is None and _CallbackHandler.error is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            server.server_close()
            raise TimeoutError("Browser login timed out — no callback received.")
        server.timeout = remaining
        server.handle_request()

    server.server_close()

    if _CallbackHandler.error:
        raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")

    # Validate state
    if _CallbackHandler.received_state != state:
        raise RuntimeError("OAuth state mismatch — possible CSRF attack.")

    auth_code = _CallbackHandler.auth_code

    # 5. Stage 1: Exchange code for tokens (form-urlencoded, matching Codex CLI)
    resp = requests.post(
        TOKEN_URL,
        data=urlencode({
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    access_token = body["access_token"]
    refresh_token = body.get("refresh_token", "")
    id_token = body.get("id_token", "")
    expires_in = body.get("expires_in", 3600)
    expires_at = int(time.time()) + int(expires_in)

    # 6. Stage 2: Exchange id_token for an API-scoped key (RFC 8693)
    api_key = _obtain_api_key(id_token)

    if not api_key:
        # Check if platform onboarding is needed
        setup_url = _check_platform_onboarding(id_token)
        if setup_url:
            print(
                "[eggllm] First-time setup required: opening OpenAI platform setup in your browser.\n"
                "[eggllm] Complete the setup, then run /login again.",
                file=sys.stderr,
            )
            webbrowser.open(setup_url)

    store.store_tokens(access_token, refresh_token, id_token, expires_at, api_key=api_key)
    return store


def logout(store: Optional[TokenStore] = None) -> None:
    """Clear stored OAuth tokens."""
    if store is None:
        store = TokenStore()
    store.clear()
