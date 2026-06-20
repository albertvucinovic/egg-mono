from __future__ import annotations

import json
from pathlib import Path

import pytest

from eggllm.capabilities import model_metadata, supports_attachment_presentation
from eggllm.client import LLMClient
from eggllm.config import load_models_config
from eggllm.registry import ModelRegistry


class DummyCatalog:
    def get_all_models_for_provider(self, provider):
        return []


def _write_models(tmp_path: Path, models: dict) -> tuple[Path, Path]:
    mpath = tmp_path / "models.json"
    apath = tmp_path / "all-models.json"
    mpath.write_text(json.dumps(models), encoding="utf-8")
    apath.write_text(json.dumps({"providers": {}}, ensure_ascii=False), encoding="utf-8")
    return mpath, apath


def test_model_metadata_defaults_preserve_legacy_chat_models():
    metadata = model_metadata({"model_name": "gpt-test"})

    assert metadata["model_kind"] == "chat"
    assert metadata["input_modalities"] == ["text"]
    assert metadata["output_modalities"] == ["text"]
    assert metadata["task_capabilities"] == ["chat"]
    assert metadata["attachment_capabilities"] == {}
    assert supports_attachment_presentation({"model_name": "gpt-test"}, "image") is False


def test_model_metadata_explicit_image_capability():
    cfg = {
        "model_name": "gpt-vision",
        "input_modalities": ["text", "image"],
        "attachment_capabilities": {"images": {"mime_types": ["image/png"]}},
    }

    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is True
    assert supports_attachment_presentation(cfg, "image", mime_type="image/webp") is False


def test_flat_supported_mime_types_enable_matching_attachment_presentations():
    cfg = {
        "model_name": "gpt-vision",
        "attachment_capabilities": {
            "supported_mime_types": ["image/png", "image/jpeg", "application/pdf"],
            "supports_inline_base64": True,
        },
    }

    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is True
    assert supports_attachment_presentation(cfg, "image", mime_type="image/webp") is False
    assert supports_attachment_presentation(cfg, "document", mime_type="application/pdf") is True


def test_non_chat_default_is_not_selected_for_conversation(tmp_path, monkeypatch):
    monkeypatch.delenv("EG_CHILD_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "default_model": "Image Generator",
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Image Generator": {"model_name": "gpt-image-1", "model_kind": "image_generation"},
                    "Chat": {"model_name": "gpt-4o"},
                },
            }
        },
    }
    mpath, apath = _write_models(tmp_path, models)

    client = LLMClient(models_path=mpath, all_models_path=apath)

    assert client.current_model_key == "Chat"


def test_non_chat_explicit_selection_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("EG_CHILD_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Chat": {"model_name": "gpt-4o"},
                    "Image Generator": {"model_name": "gpt-image-1", "model_kind": "image_generation"},
                },
            }
        },
    }
    mpath, apath = _write_models(tmp_path, models)
    client = LLMClient(models_path=mpath, all_models_path=apath)

    with pytest.raises(ValueError, match="model_kind 'image_generation'"):
        client.set_model("Image Generator")


def test_set_model_with_config_preserves_configured_capability_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("EG_CHILD_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Vision": {
                        "model_name": "gpt-4o",
                        "input_modalities": ["text", "image"],
                        "attachment_capabilities": {"images": True},
                    },
                },
            }
        },
    }
    stale_concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Vision": {"model_name": "gpt-4o"}},
            }
        }
    }
    mpath, apath = _write_models(tmp_path, models)
    client = LLMClient(models_path=mpath, all_models_path=apath)

    client.set_model_with_config("Vision", stale_concrete)

    cfg = client.registry.get_effective_model_config("Vision")
    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is True


def test_set_model_with_config_hydrates_all_model_concrete_info(tmp_path, monkeypatch):
    monkeypatch.delenv("EG_CHILD_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Chat": {"model_name": "gpt-4o"}},
            }
        },
    }
    concrete = {
        "providers": {
            "openai": {
                "api_base": "https://api.openai.com/v1/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
                "input_modalities": ["text", "image"],
                "models": {"all:openai:gpt-4o": {"model_name": "gpt-4o", "attachment_capabilities": {"images": True}}},
            }
        }
    }
    mpath, apath = _write_models(tmp_path, models)
    client = LLMClient(models_path=mpath, all_models_path=apath)

    client.set_model_with_config("all:openai:gpt-4o", concrete)

    cfg = client.registry.get_effective_model_config("all:openai:gpt-4o")
    assert cfg["model_name"] == "gpt-4o"
    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is True


def test_registry_chat_model_keys_exclude_non_chat(tmp_path):
    models = {
        "default_model": "Image Generator",
        "providers": {
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Image Generator": {"model_name": "gpt-image-1", "model_kind": "image_generation"},
                    "Chat": {"model_name": "gpt-4o"},
                },
            }
        },
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    assert registry.chat_model_keys() == ["Chat"]
    assert registry.default_chat_model_key() == "Chat"
    assert registry.get_model_metadata("Chat")["model_kind"] == "chat"


def test_provider_level_capability_metadata_defaults_models(tmp_path):
    models = {
        "providers": {
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "input_modalities": ["text", "image"],
                "attachment_capabilities": {"images": True},
                "models": {"Vision": {"model_name": "gpt-4o"}},
            }
        }
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    assert registry.get_model_metadata("Vision")["input_modalities"] == ["text", "image"]
    assert registry.get_model_metadata("Vision")["attachment_capabilities"] == {"images": True}


def test_model_attachment_capabilities_merge_provider_defaults(tmp_path):
    models = {
        "providers": {
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "input_modalities": ["text", "image"],
                "attachment_capabilities": {
                    "supported_mime_types": ["image/png", "image/jpeg"],
                    "supports_inline_base64": True,
                    "images": {"mime_types": ["image/png"], "max_size_bytes": 20},
                },
                "models": {
                    "Vision": {
                        "model_name": "gpt-4o",
                        "attachment_capabilities": {
                            "images": {"max_size_bytes": 10},
                        },
                    }
                },
            }
        }
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    caps = registry.get_model_metadata("Vision")["attachment_capabilities"]
    assert caps == {
        "supported_mime_types": ["image/png", "image/jpeg"],
        "supports_inline_base64": True,
        "images": {"mime_types": ["image/png"], "max_size_bytes": 10},
    }
    assert supports_attachment_presentation(registry.get_effective_model_config("Vision"), "image", mime_type="image/png") is True
    assert supports_attachment_presentation(registry.get_effective_model_config("Vision"), "image", mime_type="image/webp") is False
