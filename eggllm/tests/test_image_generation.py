from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from eggllm.image_generation import (
    ImageGenerationConfigError,
    ImageGenerationProviderError,
    generate_openai_images,
    resolve_openai_images_backend,
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


def _write_models(tmp_path: Path, models: dict) -> tuple[Path, Path]:
    models_path = tmp_path / "models.json"
    all_models_path = tmp_path / "all-models.json"
    models_path.write_text(json.dumps(models), encoding="utf-8")
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
