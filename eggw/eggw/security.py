"""Central API authentication and browser-origin policy for EggW."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_TOKEN_ENV = "EGGW_API_TOKEN"
ALLOWED_ORIGINS_ENV = "EGGW_ALLOWED_ORIGINS"
FRONTEND_PORT_ENV = "EGGW_FRONTEND_PORT"
AUTH_WEBSOCKET_PROTOCOL_PREFIX = "eggw.auth."
MIN_API_TOKEN_LENGTH = 32


def _header(scope: Scope, name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


def _validate_origin(origin: str) -> str:
    value = origin.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            f"Invalid origin {origin!r} in {ALLOWED_ORIGINS_ENV}; expected an http(s) origin without a path"
        )
    return value.rstrip("/")


def configured_allowed_origins(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the explicit CORS/WebSocket origin allowlist.

    The default matches the local Next.js frontend on its configured port. A
    public deployment must provide its browser-facing origin explicitly.
    """

    env = os.environ if environ is None else environ
    raw = (env.get(ALLOWED_ORIGINS_ENV) or "").strip()
    if raw:
        values = [part.strip() for part in raw.split(",") if part.strip()]
        if not values:
            raise RuntimeError(f"{ALLOWED_ORIGINS_ENV} must contain at least one origin")
        if "*" in values:
            raise RuntimeError(f"{ALLOWED_ORIGINS_ENV} cannot contain wildcard origins")
        origins = [_validate_origin(value) for value in values]
    else:
        port = (env.get(FRONTEND_PORT_ENV) or "3000").strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise RuntimeError(f"{FRONTEND_PORT_ENV} must be a valid TCP port")
        origins = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]

    # Preserve configuration order while avoiding duplicate middleware entries.
    return tuple(dict.fromkeys(origins))


def configured_api_token(environ: Mapping[str, str] | None = None) -> str:
    """Load the mandatory bearer token, failing closed on weak/missing config."""

    env = os.environ if environ is None else environ
    token = env.get(API_TOKEN_ENV) or ""
    if len(token) < MIN_API_TOKEN_LENGTH:
        raise RuntimeError(
            f"{API_TOKEN_ENV} must be set to a high-entropy token of at least "
            f"{MIN_API_TOKEN_LENGTH} characters; use eggw.sh to generate one"
        )
    if any(character.isspace() for character in token):
        raise RuntimeError(f"{API_TOKEN_ENV} must not contain whitespace")
    return token


@dataclass(frozen=True)
class SecurityConfig:
    """Immutable process security settings shared by all transports."""

    api_token: str
    allowed_origins: tuple[str, ...]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SecurityConfig":
        return cls(
            api_token=configured_api_token(environ),
            allowed_origins=configured_allowed_origins(environ),
        )

    def origin_allowed(self, origin: str | None) -> bool:
        # Non-browser clients generally omit Origin. Browser HTTP and WebSocket
        # clients send it and must match the same configured allowlist.
        return origin is None or origin.rstrip("/") in self.allowed_origins

    def bearer_valid(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, separator, credential = authorization.partition(" ")
        return bool(
            separator
            and scheme.lower() == "bearer"
            and secrets.compare_digest(credential, self.api_token)
        )

    def websocket_protocol_valid(self, protocols: Iterable[str]) -> bool:
        for protocol in protocols:
            if not protocol.startswith(AUTH_WEBSOCKET_PROTOCOL_PREFIX):
                continue
            credential = protocol[len(AUTH_WEBSOCKET_PROTOCOL_PREFIX) :]
            if secrets.compare_digest(credential, self.api_token):
                return True
        return False


class ApiAuthorizationMiddleware:
    """Authorize every EggW HTTP, SSE, and WebSocket entry point centrally."""

    def __init__(self, app: ASGIApp, *, config: SecurityConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope_type == "http" and path == "/health":
            await self.app(scope, receive, send)
            return

        origin = _header(scope, b"origin")
        if not self.config.origin_allowed(origin):
            await self._deny(scope, receive, send, status_code=403, detail="Origin not allowed")
            return
        if scope_type == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        authorized = self.config.bearer_valid(_header(scope, b"authorization"))
        if scope_type == "websocket":
            authorized = authorized or self.config.websocket_protocol_valid(scope.get("subprotocols", []))

        if not authorized:
            await self._deny(scope, receive, send, status_code=401, detail="Invalid or missing API token")
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        if scope["type"] == "websocket":
            # Closing before accept makes the handshake fail without putting a
            # credential in the URL or application logs.
            await send({"type": "websocket.close", "code": 4403 if status_code == 403 else 4401})
            return
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)
