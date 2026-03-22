import json
from pathlib import Path

from eggllm.catalog import AllModelsCatalog


def test_derive_models_url_trims_responses_endpoint(tmp_path: Path):
    catalog = AllModelsCatalog(tmp_path / "all-models.json")

    url = catalog._derive_models_url(
        "openai",
        {"api_base": "https://api.openai.com/v1/responses"},
    )

    assert url == "https://api.openai.com/v1/models"


def test_derive_models_url_rejects_chatgpt_oauth_provider(tmp_path: Path):
    catalog = AllModelsCatalog(tmp_path / "all-models.json")

    msg = catalog._derive_models_url(
        "openai-pro",
        {
            "api_base": "https://chatgpt.com/backend-api/codex/responses",
            "auth_type": "chatgpt_oauth",
        },
    )

    assert msg.startswith("Error:")
    assert "ChatGPT OAuth" in msg
    assert "/updateAllModels" in msg


def test_update_provider_uses_bearer_header_for_api_key_provider(tmp_path: Path, monkeypatch):
    catalog = AllModelsCatalog(tmp_path / "all-models.json")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    called = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "gpt-4.1"}, {"id": "gpt-4o"}]}

    def fake_get(url, headers=None, timeout=None):
        called["url"] = url
        called["headers"] = headers or {}
        called["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setattr("requests.get", fake_get)

    result = catalog.update_provider(
        "openai",
        {
            "openai": {
                "api_base": "https://api.openai.com/v1/responses",
                "api_key_env": "OPENAI_API_KEY",
            }
        },
    )

    assert result == "Updated all-models.json for provider 'openai' with 2 models."
    assert called["url"] == "https://api.openai.com/v1/models"
    assert called["headers"]["Authorization"] == "Bearer test-key"
    assert called["headers"]["Content-Type"] == "application/json"

    saved = json.loads((tmp_path / "all-models.json").read_text())
    assert saved["providers"]["openai"]["models"] == ["gpt-4.1", "gpt-4o"]