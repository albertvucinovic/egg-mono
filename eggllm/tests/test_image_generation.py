from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from eggllm.image_generation import (
    ImageGenerationConfigError,
    ImageGenerationProviderError,
    generate_images,
    generate_openai_images,
    generate_openai_responses_image_tool,
    resolve_openai_images_backend,
    resolve_openai_responses_image_tool_backend,
)
from eggllm.providers.factory import AdapterFactory


class FakeResponse:
    def __init__(self, payload=None, *, content: bytes | None = None, headers=None):
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.raise_for_status_calls = 0

    def raise_for_status(self):
        self.raise_for_status_calls += 1

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeErrorResponse(FakeResponse):
    def __init__(self, payload=None, *, text: str = "", content: bytes | None = None, headers=None):
        super().__init__(payload, content=content, headers=headers)
        self.text = text

    def raise_for_status(self):
        self.raise_for_status_calls += 1
        raise RuntimeError("400 Client Error: Bad Request for url")


class FakeSession:
    def __init__(self, post_payload, *, get_content: bytes = b"url-image", get_headers=None):
        self.post_payload = post_payload
        self.get_content = get_content
        self.get_headers = get_headers or {"Content-Type": "image/png; charset=utf-8"}
        self.posts = []
        self.gets = []

    def post(self, url, *, headers, json, timeout):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(self.post_payload)

    def get(self, url, *, timeout):
        self.gets.append({"url": url, "timeout": timeout})
        return FakeResponse(content=self.get_content, headers=self.get_headers)


_IMAGE_METADATA_KEYS = {
    "api_type",
    "model_kind",
    "task_capabilities",
    "input_modalities",
    "output_modalities",
    "attachment_capabilities",
}


def _write_models(tmp_path: Path, models: dict) -> tuple[Path, Path]:
    models_path = tmp_path / "models.json"
    all_models_path = tmp_path / "all-models.json"
    image_models_path = tmp_path / "image-generation-models.json"

    providers_for_models: dict[str, dict] = {}
    image_models: dict[str, dict] = {}
    for provider_name, provider_cfg in (models.get("providers") or {}).items():
        if not isinstance(provider_cfg, dict):
            continue
        providers_for_models[provider_name] = {
            key: value
            for key, value in provider_cfg.items()
            if key != "models" and key not in _IMAGE_METADATA_KEYS
        }
        provider_image_defaults = {
            key: value
            for key, value in provider_cfg.items()
            if key in _IMAGE_METADATA_KEYS
        }
        for display_name, model_cfg in (provider_cfg.get("models") or {}).items():
            if isinstance(model_cfg, str):
                entry = {"model_name": model_cfg}
            elif isinstance(model_cfg, dict):
                entry = dict(model_cfg)
            else:
                continue
            for key, value in provider_image_defaults.items():
                entry.setdefault(key, value)
            entry.setdefault("provider", provider_name)
            image_models[display_name] = entry

    default_model = models.get("default_model") or (next(iter(image_models)) if image_models else None)
    models_path.write_text(json.dumps({"providers": providers_for_models}), encoding="utf-8")
    image_models_path.write_text(
        json.dumps({"default_model": default_model, "models": image_models}, ensure_ascii=False),
        encoding="utf-8",
    )
    all_models_path.write_text(json.dumps({"providers": {}}, ensure_ascii=False), encoding="utf-8")
    return models_path, all_models_path


def _image_models(provider_overrides=None, model_overrides=None):
    provider = {
        "api_base": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "api_type": "openai_images",
        "model_kind": "image_generation",
        "parameters": {"quality": "medium"},
        "models": {
            "Image Backend": {
                "model_name": "gpt-image-1",
                "task_capabilities": ["image_generation"],
            },
        },
    }
    if provider_overrides:
        provider.update(provider_overrides)
    if model_overrides:
        provider["models"]["Image Backend"].update(model_overrides)
    return {"providers": {"openai-images": provider}}


def _responses_image_tool_models(provider_overrides=None, model_overrides=None):
    provider = {
        "api_base": "https://api.openai.com/v1/responses",
        "api_key_env": "OPENAI_API_KEY",
        "api_type": "openai_responses_image_tool",
        "model_kind": "image_generation",
        "parameters": {"size": "1024x1024", "store": False},
        "models": {
            "Responses Image Tool": {
                "model_name": "gpt-4.1",
                "task_capabilities": ["image_generation"],
            },
        },
    }
    if provider_overrides:
        provider.update(provider_overrides)
    if model_overrides:
        provider["models"]["Responses Image Tool"].update(model_overrides)
    return {"providers": {"openai-responses-image-tool": provider}}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_openai_images_request_auth_headers_b64_decode_and_multiple_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())
    session = FakeSession(
        {
            "id": "img-resp-123",
            "created": 123456,
            "data": [
                {"b64_json": _b64(b"first-webp"), "revised_prompt": "A refined prompt"},
                {"b64_json": _b64(b"second-webp")},
            ],
        }
    )

    result = generate_openai_images(
        "  Paint a small robot  ",
        model_key="Image Backend",
        models_path=models_path,
        all_models_path=all_models_path,
        options={
            "n": 2,
            "size": "1024x1024",
            "quality": "high",
            "output_format": "webp",
            "background": "transparent",
        },
        timeout=42,
        session=session,
    )

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "https://api.openai.com/v1/images/generations"
    assert post["timeout"] == 42
    assert post["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key",
    }
    assert post["json"] == {
        "model": "gpt-image-1",
        "prompt": "Paint a small robot",
        "quality": "high",
        "n": 2,
        "size": "1024x1024",
        "output_format": "webp",
        "background": "transparent",
    }

    assert result.model_key == "Image Backend"
    assert result.provider_name == "openai-images"
    assert result.model_name == "gpt-image-1"
    assert result.prompt == "Paint a small robot"
    assert result.request_options == {
        "quality": "high",
        "n": 2,
        "size": "1024x1024",
        "output_format": "webp",
        "background": "transparent",
    }
    assert result.response_metadata == {"id": "img-resp-123", "created": 123456}
    assert [image.data for image in result.images] == [b"first-webp", b"second-webp"]
    assert result.images[0].metadata["mime_type"] == "image/webp"
    assert result.images[0].metadata["filename"] == "generated-1.webp"
    assert result.images[0].metadata["revised_prompt"] == "A refined prompt"
    assert result.images[1].metadata["filename"] == "generated-2.webp"
    assert "b64_json" not in result.images[0].metadata


def test_openai_images_discovers_first_matching_backend_and_does_not_register_chat_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())
    session = FakeSession({"data": [{"b64_json": _b64(b"png-bytes")}]})

    backend = resolve_openai_images_backend(models_path=models_path, all_models_path=all_models_path)
    result = generate_openai_images(
        "A discovered backend",
        models_path=models_path,
        all_models_path=all_models_path,
        session=session,
    )

    assert backend.model_key == "Image Backend"
    assert result.model_key == "Image Backend"
    assert session.posts[0]["json"]["model"] == "gpt-image-1"
    assert "openai_images" not in AdapterFactory.supported_types()


def test_image_generation_models_are_loaded_from_dedicated_file_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path = tmp_path / "models.json"
    all_models_path = tmp_path / "all-models.json"
    image_models_path = tmp_path / "image-generation-models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "api_base": "https://api.openai.com/v1/chat/completions",
                        "api_key_env": "OPENAI_API_KEY",
                        "models": {
                            "Chat Only Even If It Looks Like Image": {
                                "model_name": "gpt-image-1",
                                "api_type": "openai_images",
                                "model_kind": "image_generation",
                                "task_capabilities": ["image_generation"],
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    image_models_path.write_text(
        json.dumps(
            {
                "default_model": "Dedicated Image",
                "models": {
                    "Dedicated Image": {
                        "provider": "openai",
                        "api_type": "openai_images",
                        "model_name": "gpt-image-1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    all_models_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
    session = FakeSession({"data": [{"b64_json": _b64(b"dedicated")}]})

    result = generate_images(
        "use dedicated config",
        models_path=models_path,
        all_models_path=all_models_path,
        image_generation_models_path=image_models_path,
        session=session,
    )

    assert result.model_key == "Dedicated Image"
    assert result.provider_name == "openai"
    assert session.posts[0]["url"] == "https://api.openai.com/v1/images/generations"


def test_image_generation_rejects_catalog_all_model_handles(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())
    all_models_path.write_text(json.dumps({"providers": {"openai-images": {"models": ["gpt-image-1"]}}}), encoding="utf-8")

    with pytest.raises(ImageGenerationConfigError, match="must be listed in image-generation-models.json"):
        generate_images(
            "catalog handle",
            model_key="all:openai-images:gpt-image-1",
            models_path=models_path,
            all_models_path=all_models_path,
            session=FakeSession({"data": [{"b64_json": _b64(b"unused")}]}),
        )


def test_openai_images_discovery_skips_other_image_backend_api_types(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            "responses-image-tool": {
                "api_base": "https://api.openai.com/v1/responses",
                "api_key_env": "OPENAI_API_KEY",
                "api_type": "openai_responses_image_tool",
                "model_kind": "image_generation",
                "models": {
                    "Responses Image Tool": {
                        "model_name": "gpt-4.1",
                        "task_capabilities": ["image_generation"],
                    }
                },
            },
            "openai-images": _image_models()["providers"]["openai-images"],
        }
    }
    models_path, all_models_path = _write_models(tmp_path, models)
    session = FakeSession({"data": [{"b64_json": _b64(b"png-bytes")}]})

    result = generate_openai_images(
        "Use the dedicated images backend",
        models_path=models_path,
        all_models_path=all_models_path,
        session=session,
    )

    assert result.model_key == "Image Backend"
    assert session.posts[0]["url"] == "https://api.openai.com/v1/images/generations"
    assert session.posts[0]["json"]["model"] == "gpt-image-1"


def test_openai_images_supports_url_response_when_download_is_available(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(
        tmp_path,
        _image_models(provider_overrides={"api_base": "https://api.openai.com/v1/images/generations"}),
    )
    session = FakeSession(
        {"data": [{"url": "https://cdn.example.test/generated.png"}]},
        get_content=b"downloaded-png",
        get_headers={"Content-Type": "image/png"},
    )

    result = generate_openai_images(
        "URL response",
        model_key="Image Backend",
        models_path=models_path,
        all_models_path=all_models_path,
        session=session,
        timeout=7,
    )

    assert session.posts[0]["url"] == "https://api.openai.com/v1/images/generations"
    assert session.gets == [{"url": "https://cdn.example.test/generated.png", "timeout": 7}]
    assert result.images[0].data == b"downloaded-png"
    assert result.images[0].metadata["source"] == "url"
    assert result.images[0].metadata["source_url"] == "https://cdn.example.test/generated.png"
    assert result.images[0].metadata["mime_type"] == "image/png"


@pytest.mark.parametrize(
    ("provider_overrides", "model_overrides", "message"),
    [
        ({}, {"model_kind": "chat"}, "model_kind 'chat'"),
        ({"api_type": "responses"}, {}, "api_type 'responses'"),
        ({}, {"task_capabilities": ["image_edit"]}, "task_capabilities"),
    ],
)
def test_openai_images_rejects_unsupported_backend_config(
    tmp_path,
    monkeypatch,
    provider_overrides,
    model_overrides,
    message,
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(
        tmp_path,
        _image_models(provider_overrides=provider_overrides, model_overrides=model_overrides),
    )

    with pytest.raises(ImageGenerationConfigError, match=message):
        generate_openai_images(
            "bad backend",
            model_key="Image Backend",
            models_path=models_path,
            all_models_path=all_models_path,
            session=FakeSession({"data": [{"b64_json": _b64(b"unused")}]}),
        )


def test_openai_images_rejects_unknown_explicit_options(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())

    with pytest.raises(ImageGenerationConfigError, match="Unsupported OpenAI Images option"):
        generate_openai_images(
            "bad option",
            model_key="Image Backend",
            models_path=models_path,
            all_models_path=all_models_path,
            options={"temperature": 0.7},
            session=FakeSession({"data": [{"b64_json": _b64(b"unused")}]}),
        )


def test_openai_images_provider_errors_for_invalid_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())

    with pytest.raises(ImageGenerationProviderError, match="invalid base64"):
        generate_openai_images(
            "bad b64",
            model_key="Image Backend",
            models_path=models_path,
            all_models_path=all_models_path,
            session=FakeSession({"data": [{"b64_json": "not base64!!!"}]}),
        )


def test_openai_responses_image_tool_request_and_parse_call_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _responses_image_tool_models())
    session = FakeSession(
        {
            "id": "resp_123",
            "created": 123456,
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "id": "ig_123",
                    "status": "completed",
                    "result": _b64(b"generated-png"),
                    "revised_prompt": "A refined prompt",
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )

    backend = resolve_openai_responses_image_tool_backend(
        "Responses Image Tool",
        models_path=models_path,
        all_models_path=all_models_path,
    )
    result = generate_openai_responses_image_tool(
        "  Paint an egg in space  ",
        model_key="Responses Image Tool",
        models_path=models_path,
        all_models_path=all_models_path,
        options={"quality": "high", "output_format": "png"},
        timeout=33,
        session=session,
    )

    assert backend.url == "https://api.openai.com/v1/responses"
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "https://api.openai.com/v1/responses"
    assert post["timeout"] == 33
    assert post["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-key",
    }
    assert post["json"] == {
        "model": "gpt-4.1",
        "instructions": "",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Paint an egg in space"}],
            }
        ],
        "tools": [{"type": "image_generation", "size": "1024x1024", "quality": "high", "output_format": "png"}],
        "tool_choice": "required",
        "store": False,
    }
    assert result.model_key == "Responses Image Tool"
    assert result.provider_name == "openai-responses-image-tool"
    assert result.model_name == "gpt-4.1"
    assert result.prompt == "Paint an egg in space"
    assert result.request_options == {"size": "1024x1024", "quality": "high", "output_format": "png"}
    assert result.response_metadata == {
        "api_type": "openai_responses_image_tool",
        "id": "resp_123",
        "created": 123456,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    assert result.images[0].data == b"generated-png"
    assert result.images[0].metadata["api_type"] == "openai_responses_image_tool"
    assert result.images[0].metadata["source"] == "image_generation_call"
    assert result.images[0].metadata["image_generation_call_id"] == "ig_123"
    assert result.images[0].metadata["revised_prompt"] == "A refined prompt"
    assert "result" not in result.images[0].metadata


def test_openai_responses_image_tool_ignores_single_image_n_option(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _responses_image_tool_models())
    session = FakeSession({"output": [{"type": "image_generation_call", "result": _b64(b"img")}]})

    result = generate_openai_responses_image_tool(
        "single image",
        model_key="Responses Image Tool",
        models_path=models_path,
        all_models_path=all_models_path,
        options={"n": 1, "size": "1024x1024"},
        session=session,
    )

    assert result.request_options == {"size": "1024x1024"}
    assert session.posts[0]["json"]["tools"] == [{"type": "image_generation", "size": "1024x1024"}]


def test_openai_responses_image_tool_rejects_multi_image_n_option(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _responses_image_tool_models())

    with pytest.raises(ImageGenerationConfigError, match="one image per call"):
        generate_openai_responses_image_tool(
            "two images",
            model_key="Responses Image Tool",
            models_path=models_path,
            all_models_path=all_models_path,
            options={"n": 2},
            session=FakeSession({"output": [{"type": "image_generation_call", "result": _b64(b"img")}]},),
        )


def test_openai_responses_image_tool_rejects_packaged_chatgpt_codex_backend_before_http(tmp_path, monkeypatch):
    models_path, all_models_path = _write_models(
        tmp_path,
        _responses_image_tool_models(
            provider_overrides={
                "api_base": "https://chatgpt.com/backend-api/codex/responses",
                "auth_type": "chatgpt_oauth",
                "api_key_env": "",
            },
            model_overrides={"model_name": "gpt-5.5"},
        ),
    )
    session = FakeSession({"output": [{"type": "image_generation_call", "result": _b64(b"img")}]})

    with pytest.raises(ImageGenerationConfigError, match="ChatGPT/Codex subscription Responses endpoint"):
        generate_openai_responses_image_tool(
            "cat",
            model_key="Responses Image Tool",
            models_path=models_path,
            all_models_path=all_models_path,
            session=session,
        )

    assert session.posts == []


def test_openai_image_generation_http_error_includes_response_body(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _image_models())

    class ErrorSession:
        posts = []

        def post(self, url, *, headers, json, timeout):
            self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeErrorResponse(text='{"error":{"message":"bad option"}}')

    with pytest.raises(ImageGenerationProviderError, match="bad option"):
        generate_openai_images(
            "bad",
            model_key="Image Backend",
            models_path=models_path,
            all_models_path=all_models_path,
            session=ErrorSession(),
        )


def test_generate_images_dispatches_to_responses_image_tool_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _responses_image_tool_models())
    session = FakeSession({"output": [{"type": "image_generation_call", "result": _b64(b"img")}]})

    result = generate_images(
        "dispatch",
        models_path=models_path,
        all_models_path=all_models_path,
        session=session,
    )

    assert result.model_key == "Responses Image Tool"
    assert session.posts[0]["url"] == "https://api.openai.com/v1/responses"
    assert session.posts[0]["json"]["tools"] == [{"type": "image_generation", "size": "1024x1024"}]
    assert session.posts[0]["json"]["tool_choice"] == "required"


def test_generate_images_dispatches_explicit_openai_images_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models = {
        "providers": {
            **_responses_image_tool_models()["providers"],
            **_image_models()["providers"],
        }
    }
    models_path, all_models_path = _write_models(tmp_path, models)
    session = FakeSession({"data": [{"b64_json": _b64(b"png")}]})

    result = generate_images(
        "explicit images",
        model_key="Image Backend",
        models_path=models_path,
        all_models_path=all_models_path,
        session=session,
    )

    assert result.model_key == "Image Backend"
    assert session.posts[0]["url"] == "https://api.openai.com/v1/images/generations"


def test_openai_responses_image_tool_rejects_bad_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    models_path, all_models_path = _write_models(tmp_path, _responses_image_tool_models())

    with pytest.raises(ImageGenerationProviderError, match="did not contain image_generation_call"):
        generate_openai_responses_image_tool(
            "no image",
            model_key="Responses Image Tool",
            models_path=models_path,
            all_models_path=all_models_path,
            session=FakeSession({"output": [{"type": "message", "content": []}]}),
        )
