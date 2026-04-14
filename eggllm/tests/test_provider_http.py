import json
from pathlib import Path

import pytest

from eggllm.provider_http import build_provider_headers


def test_build_provider_headers_for_api_key_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    headers = build_provider_headers(
        "openai",
        {"api_key_env": "OPENAI_API_KEY"},
        accept_sse=False,
    )

    assert headers == {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key",
    }


def test_build_provider_headers_for_chatgpt_oauth_provider(monkeypatch, tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "header.payload.sig",
                    "refresh_token": "refresh",
                    "expires_at": 32503680000,
                },
                "chatgpt_account_id": "acct_123",
            }
        )
    )

    class DummyStore:
        def __init__(self):
            self.auth_path = auth_path

        def is_logged_in(self):
            return True

        def get_access_token(self):
            return "access-123"

        def get_account_id(self):
            return "acct_123"

    monkeypatch.setattr("eggllm.provider_http.TokenStore", DummyStore, raising=False)

    headers = build_provider_headers(
        "openai-pro",
        {"auth_type": "chatgpt_oauth"},
        accept_sse=True,
    )

    assert headers["Authorization"] == "Bearer access-123"
    assert headers["chatgpt-account-id"] == "acct_123"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["accept"] == "text/event-stream"
    assert headers["User-Agent"].startswith("eggllm/1.0 (")


def test_build_provider_headers_requires_oauth_login(monkeypatch):
    class DummyStore:
        def is_logged_in(self):
            return False

    monkeypatch.setattr("eggllm.provider_http.TokenStore", DummyStore, raising=False)

    with pytest.raises(EnvironmentError):
        build_provider_headers(
            "openai-pro",
            {"auth_type": "chatgpt_oauth"},
            accept_sse=False,
        )