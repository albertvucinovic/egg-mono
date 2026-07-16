"""Behavioral security tests for EggW's process-wide transport boundary."""
from __future__ import annotations

import importlib
import json
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
    # Other EggW test modules deliberately remove only the fully-qualified
    # module entry while Python leaves the package attribute behind. Importing
    # with ``from eggw import main`` can then return that stale module without
    # re-executing its environment-derived security configuration.
    sys.modules.pop("eggw.main", None)
    main = importlib.import_module("eggw.main")
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
    from eggthreads import create_root_thread
    from eggw.core import state as core_state

    from eggthreads import ThreadsDB

    setup_db = ThreadsDB(core_state.db.path)
    thread_id = create_root_thread(setup_db, name="security-sse")
    setup_db.conn.close()
    monkeypatch.setattr("eggw.routes.events.EventSourceResponse", FiniteEventSourceResponse)
    response = client.get(f"/api/threads/{thread_id}/events", headers=auth_headers())
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


def _run_launcher(
    tmp_path: Path,
    *launcher_args: str,
    backend_body: str | None = None,
    **environment: str,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "capture"
    capture.mkdir()
    _write_executable(fake_bin / "nc", "exit 1")
    _write_executable(
        fake_bin / "curl",
        'printf "%s\\n" "$@" >> "$EGGW_TEST_CAPTURE/curl-args"\n'
        'if [ "${EGGW_TEST_CURL_FAIL:-0}" = "1" ]; then exit 7; fi\n'
        'exit 0',
    )
    default_backend_body = (
        'printf "%s\\n" "$@" > "$EGGW_TEST_CAPTURE/backend-args"\n'
        'printf "%s" "$EGGW_API_TOKEN" > "$EGGW_TEST_CAPTURE/backend-token"\n'
        'printf "%s" "$EGGW_ALLOWED_ORIGINS" > "$EGGW_TEST_CAPTURE/origins"\n'
        'printf "%s" "${EGGW_QUICK_START_ARGS_JSON:-}" > "$EGGW_TEST_CAPTURE/quick-start-json"\n'
        'sleep 3'
    )
    _write_executable(
        fake_bin / "fake-hypercorn",
        backend_body if backend_body is not None else "".join(default_backend_body),
    )
    _write_executable(
        fake_bin / "fake-npm",
        'printf "%s\\n" "$@" > "$EGGW_TEST_CAPTURE/frontend-args"\n'
        'printf "%s" "${EGGW_API_TOKEN:-}" > "$EGGW_TEST_CAPTURE/frontend-server-api-token"\n'
        'printf "%s" "${EGGW_PRIVATE_BOOTSTRAP_TOKEN:-}" > "$EGGW_TEST_CAPTURE/frontend-bootstrap-token"\n'
        'printf "%s" "${NEXT_PUBLIC_EGGW_API_TOKEN:-}" > "$EGGW_TEST_CAPTURE/frontend-public-token"\n'
        'printf "%s" "${NEXT_PUBLIC_API_URL:-}" > "$EGGW_TEST_CAPTURE/frontend-api-url"\n'
        'sleep 3',
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "EGGW_HYPERCORN_BIN": str(fake_bin / "fake-hypercorn"),
        "EGGW_NPM_BIN": str(fake_bin / "fake-npm"),
        "EGGW_SKIP_FRONTEND_WARMUP": "1",
        "EGGW_BACKEND_STARTUP_TIMEOUT": "1",
        "EGGW_NO_BROWSER": "1",
        "EGGW_BACKEND_PORT": "18123",
        "EGGW_FRONTEND_PORT": "18124",
        "EGGW_TEST_CAPTURE": str(capture),
        **environment,
    }
    for key in (
        "EGGW_API_TOKEN",
        "EGGW_ALLOWED_ORIGINS",
        "EGGW_BIND_HOST",
        "EGGW_FRONTEND_BIND_HOST",
        "EGGW_PUBLIC",
        "NEXT_PUBLIC_API_URL",
        "EGGW_RELOAD_THREAD_ID",
        "EGGW_QUICK_START_ARGS_JSON",
    ):
        if key not in environment:
            env.pop(key, None)
    return subprocess.run(
        [str(repo_root / "eggw" / "eggw.sh"), *launcher_args],
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
    frontend_token = (capture / "frontend-bootstrap-token").read_text()
    assert backend_token == frontend_token
    assert (capture / "frontend-public-token").read_text() == ""
    assert (capture / "frontend-server-api-token").read_text() == ""
    assert len(backend_token) >= 64
    assert backend_token not in result.stdout
    assert backend_token not in result.stderr
    backend_args = (capture / "backend-args").read_text().splitlines()
    assert backend_args[-2:] == ["--bind", "127.0.0.1:18123"]
    frontend_args = (capture / "frontend-args").read_text().splitlines()
    assert frontend_args[-4:] == ["-H", "127.0.0.1", "-p", "18124"]
    assert (capture / "frontend-api-url").read_text() == "http://localhost:18123"
    assert (capture / "origins").read_text() == "http://localhost:18124,http://127.0.0.1:18124"
    curl_args = (capture / "curl-args").read_text()
    assert "--noproxy\n*" in curl_args
    assert "http://127.0.0.1:18123/health" in curl_args


def test_launcher_preserves_quick_start_argument_boundaries_and_reload_suppresses_them(tmp_path: Path):
    result = _run_launcher(
        tmp_path / "fresh",
        "Tell",
        "me a story",
        'quote "inside"',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "fresh" / "capture" / "quick-start-json").read_text()) == [
        "Tell",
        "me a story",
        'quote "inside"',
    ]

    reloaded = _run_launcher(
        tmp_path / "reload",
        "must not reapply",
        EGGW_RELOAD_THREAD_ID="existing-thread",
    )
    assert reloaded.returncode == 0, reloaded.stdout + reloaded.stderr
    assert json.loads((tmp_path / "reload" / "capture" / "quick-start-json").read_text()) == []


def test_launcher_does_not_start_frontend_before_backend_is_healthy(tmp_path: Path):
    result = _run_launcher(
        tmp_path,
        backend_body='sleep 3',
        EGGW_BACKEND_STARTUP_TIMEOUT="1",
        EGGW_TEST_CURL_FAIL="1",
    )

    assert result.returncode != 0
    assert "Backend did not become healthy within 1s" in result.stderr
    assert not (tmp_path / "capture" / "frontend-args").exists()


def test_launcher_reports_backend_exit_before_starting_frontend(tmp_path: Path):
    result = _run_launcher(
        tmp_path,
        backend_body='printf "backend boom\\n" >&2\nexit 23',
        EGGW_BACKEND_STARTUP_TIMEOUT="5",
        EGGW_TEST_CURL_FAIL="1",
    )

    assert result.returncode != 0
    assert "[backend] backend boom" in result.stdout
    assert "Backend exited during startup (status 23)" in result.stderr
    assert not (tmp_path / "capture" / "frontend-args").exists()


def test_launcher_rejects_invalid_backend_startup_timeout(tmp_path: Path):
    result = _run_launcher(tmp_path, EGGW_BACKEND_STARTUP_TIMEOUT="invalid")

    assert result.returncode != 0
    assert "EGGW_BACKEND_STARTUP_TIMEOUT must be a positive integer" in result.stderr
    assert not (tmp_path / "capture" / "frontend-args").exists()


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "frontend" / "node_modules").is_dir(), reason="frontend dependencies not installed")
def test_production_browser_bundle_does_not_contain_server_token():
    repo_root = Path(__file__).resolve().parents[2]
    frontend_root = repo_root / "eggw" / "frontend"
    token = "bundle-secret-token-" + "q" * 48
    env = {
        **os.environ,
        "NEXT_PUBLIC_API_URL": "http://localhost:8000",
        "EGGW_API_TOKEN": token,
        "EGGW_PRIVATE_BOOTSTRAP_TOKEN": token,
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    browser_assets = frontend_root / ".next" / "static"
    assert browser_assets.is_dir()
    assert not any(
        token.encode() in path.read_bytes()
        for path in browser_assets.rglob("*")
        if path.is_file()
    )


def test_frontend_token_source_is_runtime_only_and_not_persisted_in_local_storage():
    frontend_root = Path(__file__).resolve().parents[1] / "frontend"
    api_source = (frontend_root / "src" / "lib" / "api.ts").read_text()
    token_source = (frontend_root / "src" / "lib" / "apiToken.ts").read_text()
    bootstrap_source = (frontend_root / "src" / "app" / "api" / "eggw-bootstrap" / "route.ts").read_text()

    assert "NEXT_PUBLIC_EGGW_API_TOKEN" not in api_source
    assert "NEXT_PUBLIC_EGGW_API_TOKEN" not in token_source
    assert "NEXT_PUBLIC_EGGW_API_TOKEN" not in bootstrap_source
    assert "localStorage" not in token_source
    assert "sessionStorage" in token_source
    assert "EGGW_PRIVATE_BOOTSTRAP_TOKEN" in bootstrap_source
    assert "x-forwarded-for" not in bootstrap_source.lower()
    assert 'Cache-Control": "no-store' in bootstrap_source


def test_launcher_requires_explicit_public_override_for_non_loopback(tmp_path: Path):
    denied = _run_launcher(tmp_path / "denied", EGGW_BIND_HOST="0.0.0.0")
    assert denied.returncode != 0
    assert "requires explicit EGGW_PUBLIC=1" in denied.stderr

    missing_token = _run_launcher(tmp_path / "missing-token", EGGW_PUBLIC="1")
    assert missing_token.returncode != 0
    assert "requires an explicit EGGW_API_TOKEN" in missing_token.stderr

    missing_origins = _run_launcher(
        tmp_path / "missing-origins",
        EGGW_PUBLIC="1",
        EGGW_API_TOKEN=TEST_TOKEN,
        NEXT_PUBLIC_API_URL="https://api.eggw.example",
    )
    assert missing_origins.returncode != 0
    assert "requires explicit EGGW_ALLOWED_ORIGINS" in missing_origins.stderr

    missing_api_url = _run_launcher(
        tmp_path / "missing-api-url",
        EGGW_PUBLIC="1",
        EGGW_API_TOKEN=TEST_TOKEN,
        EGGW_ALLOWED_ORIGINS="https://eggw.example",
    )
    assert missing_api_url.returncode != 0
    assert "requires explicit NEXT_PUBLIC_API_URL" in missing_api_url.stderr

    insecure_api_url = _run_launcher(
        tmp_path / "insecure-api-url",
        EGGW_PUBLIC="1",
        EGGW_API_TOKEN=TEST_TOKEN,
        EGGW_ALLOWED_ORIGINS="https://eggw.example",
        NEXT_PUBLIC_API_URL="http://api.eggw.example",
    )
    assert insecure_api_url.returncode != 0
    assert "must use https://" in insecure_api_url.stderr

    allowed = _run_launcher(
        tmp_path / "allowed",
        EGGW_BIND_HOST="0.0.0.0",
        EGGW_FRONTEND_BIND_HOST="0.0.0.0",
        EGGW_PUBLIC="1",
        EGGW_API_TOKEN=TEST_TOKEN,
        EGGW_ALLOWED_ORIGINS="https://eggw.example",
        NEXT_PUBLIC_API_URL="https://api.eggw.example",
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    capture = tmp_path / "allowed" / "capture"
    args = (capture / "backend-args").read_text().splitlines()
    assert args[-2:] == ["--bind", "0.0.0.0:18123"]
    frontend_args = (capture / "frontend-args").read_text().splitlines()
    assert frontend_args[-4:] == ["-H", "0.0.0.0", "-p", "18124"]
    assert (capture / "frontend-bootstrap-token").read_text() == ""
    assert (capture / "frontend-public-token").read_text() == ""
    assert (capture / "frontend-server-api-token").read_text() == ""
    assert (capture / "frontend-api-url").read_text() == "https://api.eggw.example"
    assert "http://127.0.0.1:18123/health" in (capture / "curl-args").read_text()
    assert TEST_TOKEN not in (capture / "frontend-args").read_text()
