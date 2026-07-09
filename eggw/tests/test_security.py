"""Behavioral security tests for EggW's process-wide transport boundary."""
from __future__ import annotations

import importlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from eggw.security import SecurityConfig, configured_allowed_origins

TEST_TOKEN = "test-eggw-token-" + "a" * 48
TEST_ORIGIN = "http://localhost:3000"


@pytest.fixture
def secured_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("EGGW_API_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("EGG_DB_PATH", str(tmp_path / "threads.sqlite"))
    monkeypatch.setenv("EGGW_ALLOWED_ORIGINS", TEST_ORIGIN)
    monkeypatch.setenv("EGGW_FRONTEND_PORT", "3000")
    if "eggw.main" in sys.modules:
        main = importlib.reload(sys.modules["eggw.main"])
    else:
        from eggw import main
    return main.app


@pytest.fixture
def client(secured_app):
    with TestClient(secured_app) as test_client:
        yield test_client


def auth_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}", **extra}


def test_non_health_rest_requires_api_token(client: TestClient):
    response = client.get("/api/threads")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API token"}


def test_non_health_mutation_requires_api_token(client: TestClient):
    response = client.post("/api/threads/thread-id/command", json={"command": "$ echo denied"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API token"}


def test_authenticated_rest_succeeds(client: TestClient):
    response = client.get("/api/threads", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == []


def test_health_remains_public(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_sse_requires_api_token(client: TestClient):
    response = client.get("/api/threads/thread-id/events")
    assert response.status_code == 401


def test_authenticated_sse_reaches_route(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from fastapi.responses import StreamingResponse

    class FiniteEventSourceResponse(StreamingResponse):
        def __init__(self, _generator, **_kwargs):
            async def body():
                yield b"event: connected\ndata: {}\n\n"

            super().__init__(body(), media_type="text/event-stream")

    # A finite stream avoids TestClient waiting on the production event loop;
    # reaching this replacement proves middleware accepted SSE credentials.
    monkeypatch.setattr("eggw.routes.events.EventSourceResponse", FiniteEventSourceResponse)
    response = client.get("/api/threads/thread-id/events", headers=auth_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "event: connected\ndata: {}\n\n"


def test_websocket_rejects_missing_token(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/thread-id"):
            pass
    assert exc_info.value.code == 4401


def test_websocket_accepts_authorization_header_for_non_browser_clients(client: TestClient):
    with client.websocket_connect("/ws/thread-id", headers=auth_headers()) as websocket:
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


def test_websocket_accepts_browser_subprotocol_token(client: TestClient):
    with client.websocket_connect(
        "/ws/thread-id",
        subprotocols=["eggw", f"eggw.auth.{TEST_TOKEN}"],
        headers={"origin": TEST_ORIGIN},
    ) as websocket:
        # The server must not echo the credential-bearing protocol.
        assert websocket.accepted_subprotocol == "eggw"
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


def test_disallowed_browser_origin_is_rejected_for_rest_and_websocket(client: TestClient):
    response = client.get(
        "/api/threads",
        headers=auth_headers(Origin="https://attacker.example"),
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Origin not allowed"}

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws/thread-id",
            subprotocols=["eggw", f"eggw.auth.{TEST_TOKEN}"],
            headers={"origin": "https://attacker.example"},
        ):
            pass
    assert exc_info.value.code == 4403


def test_cors_allows_configured_origin_and_not_arbitrary_origin(client: TestClient):
    allowed = client.options(
        "/api/threads",
        headers={
            "Origin": TEST_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == TEST_ORIGIN

    denied = client.options(
        "/api/threads",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_api_token_is_not_logged_on_authentication_failure(client: TestClient, capsys: pytest.CaptureFixture[str]):
    rejected_token = "rejected-token-" + "z" * 48
    response = client.get("/api/threads", headers={"Authorization": f"Bearer {rejected_token}"})
    assert response.status_code == 401
    captured = capsys.readouterr()
    assert rejected_token not in captured.out
    assert rejected_token not in captured.err


def test_security_config_fails_closed_and_defaults_to_local_origins():
    with pytest.raises(RuntimeError, match="EGGW_API_TOKEN"):
        SecurityConfig.from_env({})
    assert configured_allowed_origins({"EGGW_FRONTEND_PORT": "3456"}) == (
        "http://localhost:3456",
        "http://127.0.0.1:3456",
    )
    with pytest.raises(RuntimeError, match="wildcard"):
        configured_allowed_origins({"EGGW_ALLOWED_ORIGINS": "*"})


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_launcher(tmp_path: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "capture"
    capture.mkdir()
    _write_executable(fake_bin / "nc", "exit 1")
    _write_executable(
        fake_bin / "fake-hypercorn",
        'printf "%s\\n" "$@" > "$EGGW_TEST_CAPTURE/backend-args"\n'
        'printf "%s" "$EGGW_API_TOKEN" > "$EGGW_TEST_CAPTURE/backend-token"\n'
        'printf "%s" "$EGGW_ALLOWED_ORIGINS" > "$EGGW_TEST_CAPTURE/origins"\n'
        'sleep 3',
    )
    _write_executable(
        fake_bin / "fake-npm",
        'printf "%s\\n" "$@" > "$EGGW_TEST_CAPTURE/frontend-args"\n'
        'printf "%s" "$NEXT_PUBLIC_EGGW_API_TOKEN" > "$EGGW_TEST_CAPTURE/frontend-token"\n'
        'sleep 3',
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "EGGW_HYPERCORN_BIN": str(fake_bin / "fake-hypercorn"),
        "EGGW_NPM_BIN": str(fake_bin / "fake-npm"),
        "EGGW_SKIP_FRONTEND_WARMUP": "1",
        "EGGW_NO_BROWSER": "1",
        "EGGW_BACKEND_PORT": "18123",
        "EGGW_FRONTEND_PORT": "18124",
        "EGGW_TEST_CAPTURE": str(capture),
        **environment,
    }
    for key in ("EGGW_API_TOKEN", "EGGW_ALLOWED_ORIGINS", "EGGW_BIND_HOST", "EGGW_PUBLIC"):
        if key not in environment:
            env.pop(key, None)
    return subprocess.run(
        [str(repo_root / "eggw" / "eggw.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_launcher_generates_shared_high_entropy_token_and_binds_loopback(tmp_path: Path):
    result = _run_launcher(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    capture = tmp_path / "capture"
    backend_token = (capture / "backend-token").read_text()
    frontend_token = (capture / "frontend-token").read_text()
    assert backend_token == frontend_token
    assert len(backend_token) >= 64
    assert backend_token not in result.stdout
    assert backend_token not in result.stderr
    backend_args = (capture / "backend-args").read_text().splitlines()
    assert backend_args[-2:] == ["--bind", "127.0.0.1:18123"]
    assert (capture / "origins").read_text() == "http://localhost:18124,http://127.0.0.1:18124"


def test_launcher_requires_explicit_public_override_for_non_loopback(tmp_path: Path):
    denied = _run_launcher(tmp_path / "denied", EGGW_BIND_HOST="0.0.0.0")
    assert denied.returncode != 0
    assert "requires explicit EGGW_PUBLIC=1" in denied.stderr

    allowed = _run_launcher(
        tmp_path / "allowed",
        EGGW_BIND_HOST="0.0.0.0",
        EGGW_PUBLIC="1",
        EGGW_API_TOKEN=TEST_TOKEN,
        EGGW_ALLOWED_ORIGINS="https://eggw.example",
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    args = (tmp_path / "allowed" / "capture" / "backend-args").read_text().splitlines()
    assert args[-2:] == ["--bind", "0.0.0.0:18123"]
