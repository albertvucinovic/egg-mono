from __future__ import annotations

import json
from pathlib import Path

import pytest

from eggllm.capabilities import (
    is_image_generation_model,
    is_model_kind,
    model_metadata,
    supports_attachment_presentation,
    supports_task_capability,
    task_capabilities,
)
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


def test_model_metadata_defaults_optimistically_allow_images_for_chat_models():
    metadata = model_metadata({"model_name": "gpt-test"})

    assert metadata["model_kind"] == "chat"
    assert metadata["input_modalities"] == ["text", "image"]
    assert metadata["output_modalities"] == ["text"]
    assert metadata["task_capabilities"] == ["chat"]
    assert metadata["attachment_capabilities"] == {}
    assert supports_attachment_presentation({"model_name": "gpt-test"}, "image") is True
    assert supports_task_capability({"model_name": "gpt-test"}, "chat") is True
    assert supports_task_capability({"model_name": "gpt-test"}, "image_generation") is False


def test_explicit_text_only_model_disables_image_attachments():
    cfg = {"model_name": "text-only", "input_modalities": ["text"]}

    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is False


def test_explicit_image_capability_false_disables_default_image_support():
    cfg = {"model_name": "text-only", "attachment_capabilities": {"images": False}}

    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is False


def test_model_kind_and_task_capability_helpers_normalize_tokens():
    cfg = {
        "model_name": "gpt-image-1",
        "api_type": "openai_images",
        "model_kind": "image-generation",
        "task_capabilities": ["image-generation", "image_edit", "image_generation"],
    }

    assert is_model_kind(cfg, "image_generation") is True
    assert is_image_generation_model(cfg) is True
    assert task_capabilities(cfg) == ["image_generation", "image_edit"]
    assert supports_task_capability(cfg, "image_generation") is True
    assert supports_task_capability(cfg, "image-edit") is True
    assert supports_task_capability(cfg, "chat") is False


def test_non_chat_model_kind_defaults_to_matching_task_capability():
    cfg = {"model_name": "gpt-image-1", "model_kind": "image_generation"}

    assert task_capabilities(cfg) == ["image_generation"]
    assert supports_task_capability(cfg, "image_generation") is True
    assert supports_task_capability(cfg, "chat") is False


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


def test_attachment_capability_rejects_mime_outside_presentation():
    cfg = {
        "model_name": "gpt-docs",
        "input_modalities": ["text", "document"],
        "attachment_capabilities": {"documents": True},
    }

    assert supports_attachment_presentation(cfg, "document", mime_type="application/pdf") is True
    assert supports_attachment_presentation(cfg, "document", mime_type="image/png") is False


def test_explicit_file_capability_is_mime_scoped_when_configured():
    cfg = {
        "model_name": "gpt-files",
        "input_modalities": ["text", "file"],
        "attachment_capabilities": {"files": {"mime_types": ["text/csv"]}},
    }

    assert supports_attachment_presentation(cfg, "file", mime_type="text/csv") is True
    assert supports_attachment_presentation(cfg, "file", mime_type="application/pdf") is False


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


def test_registry_discovers_image_generation_backends_by_task_and_kind(tmp_path):
    models = {
        "providers": {
            "openai-images": {
                "api_base": "https://api.openai.com/v1/images/generations",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Image Generator": {
                        "model_name": "gpt-image-1",
                        "api_type": "openai_images",
                        "model_kind": "image_generation",
                        "task_capabilities": ["image_generation", "image_edit"],
                    },
                },
            },
            "openai-responses-image-tool": {
                "api_base": "https://api.openai.com/v1/responses",
                "api_key_env": "OPENAI_API_KEY",
                "models": {
                    "Responses Image Tool": {
                        "model_name": "gpt-4.1",
                        "api_type": "openai_responses_image_tool",
                        "model_kind": "image_generation",
                        "task_capabilities": ["image_generation"],
                    },
                },
            },
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Chat": {"model_name": "gpt-4o"}},
            },
        }
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    assert registry.chat_model_keys() == ["Chat"]
    assert registry.model_keys_by_kind("image-generation") == ["Image Generator", "Responses Image Tool"]
    assert registry.task_model_keys("image_generation", model_kind="image_generation") == ["Image Generator", "Responses Image Tool"]
    assert registry.task_model_keys("image_edit", model_kind="image_generation") == ["Image Generator"]
    assert registry.get_effective_model_config("Image Generator")["api_type"] == "openai_images"
    assert registry.get_effective_model_config("Responses Image Tool")["api_type"] == "openai_responses_image_tool"


def test_provider_level_image_generation_metadata_defaults_string_models(tmp_path):
    models = {
        "default_model": "Image Backend",
        "providers": {
            "openai-images": {
                "api_base": "https://api.openai.com/v1/images/generations",
                "api_key_env": "OPENAI_API_KEY",
                "api_type": "openai_images",
                "model_kind": "image_generation",
                "task_capabilities": ["image_generation", "image_edit"],
                "models": {"Image Backend": "gpt-image-1"},
            },
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "models": {"Chat": "gpt-4o"},
            },
        },
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    assert registry.default_chat_model_key() == "Chat"
    assert registry.chat_model_keys() == ["Chat"]
    assert registry.model_keys_by_kind("image_generation") == ["Image Backend"]
    assert registry.task_model_keys("image_generation", model_kind="image_generation") == ["Image Backend"]
    cfg = registry.get_effective_model_config("Image Backend")
    assert cfg["api_type"] == "openai_images"
    assert model_metadata(cfg)["task_capabilities"] == ["image_generation", "image_edit"]


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


def test_model_level_text_only_overrides_provider_image_default(tmp_path):
    models = {
        "providers": {
            "openai": {
                "api_base": "x",
                "api_key_env": "OPENAI_API_KEY",
                "input_modalities": ["text", "image"],
                "attachment_capabilities": {"images": True},
                "models": {"Text Only": {"model_name": "text-only", "input_modalities": ["text"]}},
            }
        }
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    cfg = registry.get_effective_model_config("Text Only")
    assert cfg["input_modalities"] == ["text"]
    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is False


def test_model_level_image_modality_overrides_provider_image_disable(tmp_path):
    models = {
        "providers": {
            "local": {
                "api_base": "x",
                "api_key_env": "LOCAL_API_KEY",
                "input_modalities": ["text"],
                "attachment_capabilities": {"images": False},
                "models": {"Vision": {"model_name": "vision", "input_modalities": ["text", "image"]}},
            }
        }
    }
    mpath, _apath = _write_models(tmp_path, models)
    models_config, providers_config = load_models_config(mpath)
    registry = ModelRegistry(models_config, providers_config, DummyCatalog())

    cfg = registry.get_effective_model_config("Vision")
    assert cfg["input_modalities"] == ["text", "image"]
    assert supports_attachment_presentation(cfg, "image", mime_type="image/png") is True


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
