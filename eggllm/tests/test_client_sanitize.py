from eggllm.client import LLMClient
from eggllm.config import load_models_config
from eggllm.catalog import AllModelsCatalog

from pathlib import Path
import json
import pytest

# Minimal in-memory setup for sanitizer behavior via client internals

def test_library_constructs(tmp_path: Path):
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"OpenAI GPT-4o": {"model_name": "gpt-4o"}}
            }
        }
    }
    mpath = tmp_path / "models.json"
    apath = tmp_path / "all-models.json"
    mpath.write_text(json.dumps(models))
    apath.write_text(json.dumps({"providers": {}}))

    client = LLMClient(models_path=mpath, all_models_path=apath)
    assert client.current_model_key in client.registry.models_config


class _CaptureAdapter:
    def __init__(self):
        self.payloads = []

    def stream(self, url, headers, payload, timeout=600):
        self.payloads.append(payload)
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}}

    async def stream_async(self, url, headers, payload, timeout=600):
        self.payloads.append(payload)
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}}


def _make_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Test": {"model_name": "gpt-test"}},
            }
        }
    }
    mpath = tmp_path / "models.json"
    apath = tmp_path / "all-models.json"
    mpath.write_text(json.dumps(models))
    apath.write_text(json.dumps({"providers": {}}))
    return LLMClient(models_path=mpath, all_models_path=apath)


def test_stream_chat_strips_usage_metadata_from_provider_payload(tmp_path: Path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    adapter = _CaptureAdapter()
    client._get_adapter_for_current_model = lambda: adapter

    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "answer",
            "api_usage": {"total_input_tokens": 1},
            "provider_usage": {"prompt_tokens": 1},
        },
    ]

    list(client.stream_chat(messages))

    sent_messages = adapter.payloads[0]["messages"]
    assert all("api_usage" not in m for m in sent_messages)
    assert all("provider_usage" not in m for m in sent_messages)


def test_astream_chat_strips_usage_metadata_from_provider_payload(tmp_path: Path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    adapter = _CaptureAdapter()
    client._get_adapter_for_current_model = lambda: adapter

    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "answer",
            "api_usage": {"total_input_tokens": 1},
            "provider_usage": {"prompt_tokens": 1},
        },
    ]

    async def collect():
        return [event async for event in client.astream_chat(messages)]

    import asyncio

    asyncio.run(collect())

    sent_messages = adapter.payloads[0]["messages"]
    assert all("api_usage" not in m for m in sent_messages)
    assert all("provider_usage" not in m for m in sent_messages)


def test_send_context_only_strips_usage_metadata_from_provider_payload(tmp_path: Path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    payloads = []

    class _Response:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None):
        payloads.append(json)
        return _Response()

    monkeypatch.setattr("requests.post", fake_post)
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "answer",
            "api_usage": {"total_input_tokens": 1},
            "provider_usage": {"prompt_tokens": 1},
        },
    ]

    client.send_context_only(messages, "context")

    sent_messages = payloads[0]["messages"]
    assert all("api_usage" not in m for m in sent_messages)
    assert all("provider_usage" not in m for m in sent_messages)


def test_approximate_thread_cost_uses_cache_creation_tiers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Test": {
                        "model_name": "gpt-test",
                        "cost": {
                            "input_tokens": 1.0,
                            "cached_input": 0.1,
                            "cache_creation_input": 2.0,
                            "cache_creation_5m_input": 3.0,
                            "output_tokens": 4.0,
                        },
                    }
                },
            }
        }
    }
    mpath = tmp_path / "models.json"
    apath = tmp_path / "all-models.json"
    mpath.write_text(json.dumps(models))
    apath.write_text(json.dumps({"providers": {}}))
    client = LLMClient(models_path=mpath, all_models_path=apath)

    cost = client.approximate_thread_cost({
        "total_input_tokens": 1000,
        "cached_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "cache_creation_5m_input_tokens": 40,
        "total_output_tokens": 50,
    })

    assert cost["input"] == pytest.approx(300 / 1_000_000)
    assert cost["cached"] == pytest.approx(600 * 0.1 / 1_000_000)
    assert cost["cache_creation"] == pytest.approx(((60 * 2.0) + (40 * 3.0)) / 1_000_000)
    assert cost["output"] == pytest.approx(50 * 4.0 / 1_000_000)


def test_packaged_openai_configs_request_streaming_usage():
    import importlib.resources as resources

    with resources.files("eggconfig.eggconfig.data").joinpath("models.json").open("r", encoding="utf-8") as f:
        packaged = json.load(f)
    example = json.loads(Path("eggllm/models.json.example").read_text())

    for data in (packaged, example):
        openai_params = data["providers"]["openai"].get("parameters") or {}
        assert openai_params["stream_options"]["include_usage"] is True


def test_packaged_openai_pro_requests_cache_key_with_store_false():
    import importlib.resources as resources

    with resources.files("eggconfig.eggconfig.data").joinpath("models.json").open("r", encoding="utf-8") as f:
        packaged = json.load(f)

    params = packaged["providers"]["openai-pro"]["parameters"]
    assert params["store"] is False
    assert params["prompt_cache_key"] == "prompt_cache_key"
    assert "prompt_cache_retention" not in params
